
"""
Script to train the a GQN on the face3d dataset
"""

import collections, os, io
from PIL import Image
import torch
from torchvision.transforms import Resize, Compose, ToTensor
from torch.utils.data import Dataset
from skimage import io as skio
import numpy as np
import pickle, gzip 

import sys
import random
import math
import argparse
from tqdm import tqdm
from datetime import datetime 

import torch
import torch.nn as nn
from torch.distributions import Normal
from torch.utils.data import DataLoader
from torchvision.utils import save_image

sys.path.append("../gqn-wohlert")
from gqn import GenerativeQueryNetwork
from datasets import Face3D, transform_viewpoint

cuda = torch.cuda.is_available()
device = torch.device("cuda:0" if cuda else "cpu")


def transform_viewpoint(v):
    return v / 60.0

def resize_img(img, s=64):
    transform = Compose([Resize(size=(s,s)), ToTensor()])
    return transform(img)

class Face3D(Dataset):
    def __init__(self, root_dir, n_imgs=15, resize=64, 
                transform=None, target_transform=None, sample_type='angle_random'):
        self.root_dir = root_dir 
        self.folder_names = [x for x in os.listdir(os.path.join(self.root_dir)) if x.startswith("face")]
        self.n_imgs = n_imgs 
        self.transform = transform
        self.target_transform = target_transform
        self.resize = 64
        self.sample_type = sample_type

    def __len__(self):
        return len(self.folder_names)

    def __getitem__(self, idx):
        face_path = os.path.join(self.root_dir, self.folder_names[idx], self.sample_type)
        files = [x for x in os.listdir(face_path) if x.endswith(".jpg")]
        if self.n_imgs == 'all':
            use = np.arange(len(files))
        else:
            use = np.random.choice(len(files), self.n_imgs, replace=False) 
        images = []
        viewpoints = []
        for i in use:
            f = files[i]
            vp = np.array([float(x) for x in f.strip('.jpg').split("_")[-3:]])
            img = Image.open(os.path.join(face_path, f)) 
            img = resize_img(img, self.resize)
            #img = ToTensor()(img)
            images.append(img) 
            viewpoints.append(vp)
        images = torch.stack(images)
        viewpoints = np.stack(viewpoints)
        viewpoints = torch.from_numpy(viewpoints).type('torch.FloatTensor')
        # print(images.shape)
        # print(viewpoints.shape)
        # print(images.dtype)
        # print(viewpoints.dtype)

        if self.transform:
            images = self.transform(images)

        if self.target_transform:
            viewpoints = self.target_transform(viewpoints)

        return images, viewpoints




if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generative Query Network on Shepard Metzler Example')
    parser.add_argument('--gradient_steps', type=int, default=2*(10**6), help='number of gradient steps to run (default: 2 million)')
    parser.add_argument('--batch_size', type=int, default=36, help='size of batch (default: 36)')
    parser.add_argument('--data_dir', type=str, help='location of training data', default="train")
    parser.add_argument('--workers', type=int, help='number of data loading workers', default=2)
    parser.add_argument('--fp16', type=bool, help='whether to use FP16 (default: False)', default=False)
    parser.add_argument('--data_parallel', type=bool, help='whether to parallelise based on data (default: False)', default=False)
    parser.add_argument('--output_dir', type=str, help='location of model output', default="./output")
    parser.add_argument('--save_every', type=int, help='save models every n updates', default=100)
    parser.add_argument('--print_every', type=int, help='print output every n updates', default=10)
    parser.add_argument('--resume', type=str, help='location of previous model output', default=None)
    parser.add_argument('--no_annealing', dest='annealing', action='store_false', help='whether to anneal lr and pixel variance')
    parser.set_defaults(annealing=True)

    args = parser.parse_args()

    dataset = Face3D(root_dir=args.data_dir, n_imgs=15, target_transform=transform_viewpoint)
    print("\ntotal number of samples: {}\n".format(len(dataset)))

    # Pixel variance
    sigma_f, sigma_i = 0.7, 2.0

    # Learning rate
    mu_f, mu_i = 5*10**(-5), 5*10**(-4)
    mu, sigma = mu_f, sigma_f

    # Load the dataset
    kwargs = {'num_workers': args.workers, 'pin_memory': True} if cuda else {}
    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True, **kwargs)
    

    if not args.resume is None:
        model = torch.load(args.resume)
        log_dir = os.path.dirname(os.path.dirname(args.resume)) ## model-xxxx/checkpoints/model-xxx.pt
        log_file = os.path.join(log_dir, 'log.txt')
        s = int(os.path.basename(args.resume).split(".")[-2].split("-")[-1])
        print(s)

    else:
        # Create model and optimizer
        model = GenerativeQueryNetwork(x_dim=3, v_dim=3, r_dim=512, h_dim=128, z_dim=64, L=10, pool=True).to(device)
        if not os.path.exists(args.output_dir):
            os.mkdir(args.output_dir)
        model_name = 'gqn-face3d-' + datetime.now().strftime("%Y%m%d-%H%M%S")
        log_dir = os.path.join(args.output_dir, model_name)
        os.mkdir(log_dir)
        log_file = os.path.join(log_dir, 'log.txt')
        with open(log_file, "w") as f:
            f.write("step, nll, kl\n")
        # Number of gradient steps
        s = 0
    
    checkpoint_dir = os.path.join(log_dir, "checkpoints")
    representation_dir = os.path.join(log_dir, "representation")
    reconstruction_dir = os.path.join(log_dir, "reconstruction")
    if not os.path.exists(checkpoint_dir): 
        os.mkdir(checkpoint_dir)
    if not os.path.exists(representation_dir): 
        os.mkdir(representation_dir)
    if not os.path.exists(reconstruction_dir): 
        os.mkdir(reconstruction_dir)
        

    # Model optimisations
    model = nn.DataParallel(model) if args.data_parallel else model
    model = model.half() if args.fp16 else model

    optimizer = torch.optim.Adam(model.parameters(), lr=mu)

    
    while True:
        if s >= args.gradient_steps:
            torch.save(model, os.path.join(checkpoint_dir, "model-final.pt"))
            break

        for x, v in tqdm(loader):
            if args.fp16:
                x, v = x.half(), v.half()

            x = x.to(device)
            v = v.to(device)


            x_mu, x_q, r, kld = model(x, v)

            # If more than one GPU we must take new shape into account
            batch_size = x_q.size(0)

            # Negative log likelihood
            nll = -Normal(x_mu, sigma).log_prob(x_q)

            reconstruction = torch.mean(nll.view(batch_size, -1), dim=0).sum()
            kl_divergence  = torch.mean(kld.view(batch_size, -1), dim=0).sum()

            # Evidence lower bound
            elbo = reconstruction + kl_divergence
            elbo.backward()

            optimizer.step()
            optimizer.zero_grad()

            s += 1

            if args.annealing:
                # Anneal learning rate
                mu = max(mu_f + (mu_i - mu_f)*(1 - s/(1.6 * 10**6)), mu_f)
                for group in optimizer.param_groups:
                    group["lr"] = mu * math.sqrt(1 - 0.999**s)/(1 - 0.9**s)

                # Anneal pixel variance
                sigma = max(sigma_f + (sigma_i - sigma_f)*(1 - s/(2 * 10**5)), sigma_f)

            # Save a checkpoint 
            if s % args.save_every == 0:
                torch.save(model, os.path.join(checkpoint_dir, "model-{}.pt".format(s)))

            if s % args.print_every == 0:
                with torch.no_grad():
                    print("|Steps: {}\t|NLL: {}\t|KL: {}\t|".format(s, reconstruction.item(), kl_divergence.item()))
                    with open(log_file, 'a') as f:
                        f.write("{}, {}, {}\n".format(s, reconstruction.item(), kl_divergence.item()))

                    x, v = next(iter(loader))
                    x, v = x.to(device), v.to(device)

                    x_mu, _, r, _ = model(x, v)

                    r = r.view(-1, 1, 16, 16)

                    save_image(r.float(), os.path.join(representation_dir, "representation-{}.jpg".format(s)))
                    save_image(x_mu.float(), os.path.join(reconstruction_dir, "reconstruction-{}.jpg".format(s)))

        

