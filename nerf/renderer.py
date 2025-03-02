import math
import trimesh
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

from torch_ngp import raymarching
from .utils import custom_meshgrid, get_rays

MAX_DISTANCE = 100.0
NEAR = 0.05
FAR = 10.0


def sample_pdf(bins, weights, n_samples, det=False):
    # This implementation is from NeRF
    # bins: [B, T], old_z_vals
    # weights: [B, T - 1], bin weights.
    # return: [B, n_samples], new_z_vals

    # Get pdf
    weights = weights + 1e-5  # prevent nans
    pdf = weights / torch.sum(weights, -1, keepdim=True)
    cdf = torch.cumsum(pdf, -1)
    cdf = torch.cat([torch.zeros_like(cdf[..., :1]), cdf], -1)
    # Take uniform samples
    if det:
        u = torch.linspace(0. + 0.5 / n_samples,
                           1. - 0.5 / n_samples,
                           steps=n_samples).to(weights.device)
        u = u.expand(list(cdf.shape[:-1]) + [n_samples])
    else:
        u = torch.rand(list(cdf.shape[:-1]) + [n_samples]).to(weights.device)

    # Invert CDF
    u = u.contiguous()
    inds = torch.searchsorted(cdf, u, right=True)
    below = torch.max(torch.zeros_like(inds - 1), inds - 1)
    above = torch.min((cdf.shape[-1] - 1) * torch.ones_like(inds), inds)
    inds_g = torch.stack([below, above], -1)  # (B, n_samples, 2)

    matched_shape = [inds_g.shape[0], inds_g.shape[1], cdf.shape[-1]]
    cdf_g = torch.gather(cdf.unsqueeze(1).expand(matched_shape), 2, inds_g)
    bins_g = torch.gather(bins.unsqueeze(1).expand(matched_shape), 2, inds_g)

    denom = (cdf_g[..., 1] - cdf_g[..., 0])
    denom = torch.where(denom < 1e-5, torch.ones_like(denom), denom)
    t = (u - cdf_g[..., 0]) / denom
    samples = bins_g[..., 0] + t * (bins_g[..., 1] - bins_g[..., 0])

    return samples


def plot_pointcloud(pc, color=None):
    # pc: [N, 3]
    # color: [N, 3/4]
    print('[visualize points]', pc.shape, pc.dtype, pc.min(0), pc.max(0))
    pc = trimesh.PointCloud(pc, color)
    # axis
    axes = trimesh.creation.axis(axis_length=4)
    # sphere
    sphere = trimesh.creation.icosphere(radius=1)
    trimesh.Scene([pc, axes, sphere]).show()


class NeRFRenderer(nn.Module):

    def __init__(
        self,
        bound=1,
        cuda_ray=False,
        density_scale=1,  # scale up deltas (or sigmas), to make the density grid more sharp. larger value than 1 usually improves performance.
        min_near=0.2,
        density_thresh=0.01,
        bg_radius=-1,
    ):
        super().__init__()

        self.bound = bound
        self.cascade = 1 + math.ceil(math.log2(bound))
        self.grid_size = 128
        self.density_scale = density_scale
        self.min_near = min_near
        self.density_thresh = density_thresh
        self.bg_radius = bg_radius  # radius of the background sphere.

        # prepare aabb with a 6D tensor (xmin, ymin, zmin, xmax, ymax, zmax)
        # NOTE: aabb (can be rectangular) is only used to generate points, we still rely on bound (always cubic) to calculate density grid and hashing.
        if hasattr(bound, 'shape'):
            max_bound = np.abs(bound[1] - bound[0]).max()
            aabb_train = torch.FloatTensor([
                -max_bound, -max_bound, -max_bound, max_bound, max_bound,
                max_bound
            ])
            self.bound = max_bound
        else:
            aabb_train = torch.FloatTensor(
                [-bound, -bound, -bound, bound, bound, bound])
            self.bound = bound

        self.cascade = 1 + math.ceil(math.log2(self.bound))
        self.density_scale = density_scale

        aabb_infer = aabb_train.clone()
        self.register_buffer('aabb_train', aabb_train)
        self.register_buffer('aabb_infer', aabb_infer)

        # extra state for cuda raymarching
        self.cuda_ray = cuda_ray
        if cuda_ray:
            # density grid
            density_grid = torch.zeros([self.cascade,
                                        self.grid_size**3])  # [CAS, H * H * H]
            density_bitfield = torch.zeros(
                self.cascade * self.grid_size**3 // 8,
                dtype=torch.uint8)  # [CAS * H * H * H // 8]
            self.register_buffer('density_grid', density_grid)
            self.register_buffer('density_bitfield', density_bitfield)
            self.mean_density = 0
            self.iter_density = 0
            # step counter
            step_counter = torch.zeros(
                16, 2, dtype=torch.int32)  # 16 is hardcoded for averaging...
            self.register_buffer('step_counter', step_counter)
            self.mean_count = 0
            self.local_step = 0

    def forward(self, x, d):
        raise NotImplementedError()

    # separated density and color query (can accelerate non-cuda-ray mode.)
    def density(self, x):
        raise NotImplementedError()

    def color(self, x, d, mask=None, **kwargs):
        raise NotImplementedError()

    def reset_extra_state(self):
        if not self.cuda_ray:
            return
        # density grid
        self.density_grid.zero_()
        self.mean_density = 0
        self.iter_density = 0
        # step counter
        self.step_counter.zero_()
        self.mean_count = 0
        self.local_step = 0

    def render_from_given_pose(self,
                               c2w,
                               intrinsics,
                               H,
                               W,
                               staged=False,
                               max_ray_batch=4096,
                               bg_color=None,
                               perturb=False,
                               **kwargs):
        r"""NOTE: It is assumed that the input W_T_C pose is already in NeRF
        format.
        """
        assert (c2w.shape == (4, 4))

        # Construct rays from given camera pose.
        rays = get_rays(poses=c2w[None], intrinsics=intrinsics, H=H, W=W, N=-1)
        rays_o = rays['rays_o']
        rays_d = rays['rays_d']
        direction_norms = rays['direction_norms']
        assert (rays_o.ndim == 3 and rays_d.ndim == 3 and rays_o.shape[0] ==
                rays_d.shape[0] == direction_norms.shape[0] == 1 and
                rays_o.shape[-1] == rays_d.shape[-1] == 3)

        # Use the obtained rays to render.
        return self.render(rays_o=rays_o,
                           rays_d=rays_d,
                           direction_norms=direction_norms,
                           staged=staged,
                           max_ray_batch=max_ray_batch,
                           bg_color=bg_color,
                           perturb=perturb,
                           **kwargs), rays_o, rays_d

    def run(self,
            rays_o,
            rays_d,
            direction_norms,
            num_steps=256,
            upsample_steps=0,
            bg_color=None,
            perturb=False,
            **kwargs):
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # bg_color: [3] in range [0, 1]
        # return: image: [B, N, 3], depth: [B, N]
        assert (upsample_steps == 0)
        assert (bg_color is None)

        prefix = rays_o.shape[:-1]
        rays_o = rays_o.contiguous().view(-1, 3)
        rays_d = rays_d.contiguous().view(-1, 3)
        direction_norms = direction_norms.contiguous().view(-1)

        N = rays_o.shape[0]  # N = B * N, in fact
        device = rays_o.device

        # choose aabb
        aabb = self.aabb_train if self.training else self.aabb_infer

        # sample steps
        nears, fars = raymarching.near_far_from_aabb(rays_o, rays_d, aabb,
                                                     self.min_near)
        nears.unsqueeze_(-1)
        fars.unsqueeze_(-1)

        z_vals = torch.linspace(0.0, 1.0, num_steps,
                                device=device).unsqueeze(0)  # [1, T]
        z_vals = z_vals.expand((N, num_steps))  # [N, T]
        z_vals = nears + (fars - nears) * z_vals  # [N, T], in [nears, fars]

        # perturb z_vals
        sample_dist = (fars - nears) / num_steps
        if perturb:
            z_vals = z_vals + (torch.rand(z_vals.shape, device=device) -
                               0.5) * sample_dist

        # generate xyzs
        xyzs = rays_o.unsqueeze(-2) + rays_d.unsqueeze(-2) * z_vals.unsqueeze(
            -1)  # [N, 1, 3] * [N, T, 1] -> [N, T, 3]
        xyzs = torch.min(torch.max(xyzs, aabb[:3]), aabb[3:])  # a manual clip.

        # query density and RGB
        density_outputs = self.density(xyzs.reshape(-1, 3))

        for k, v in density_outputs.items():
            density_outputs[k] = v.view(N, num_steps, -1)

        # upsample z_vals (nerf-like)
        upsample_step = 0

        deltas = z_vals[..., 1:] - z_vals[..., :-1]  # [N, T+t-1]
        deltas = torch.cat(
            [deltas, sample_dist * torch.ones_like(deltas[..., :1])], dim=-1)
        alphas = 1 - torch.exp(-deltas * self.density_scale *
                               density_outputs['sigma'].squeeze(-1))  # [N, T+t]
        alphas_shifted = torch.cat(
            [torch.ones_like(alphas[..., :1]), 1 - alphas + 1e-15],
            dim=-1)  # [N, T+t+1]
        weights = alphas * torch.cumprod(alphas_shifted,
                                         dim=-1)[..., :-1]  # [N, T+t]

        dirs = rays_d.view(-1, 1, 3).expand_as(xyzs)

        mask = weights > 1e-4  # hard coded
        geometric_features = density_outputs['geo_feat']
        sigma = density_outputs['sigma'].view(-1, 1)
        rgbs = self.color(xyzs.reshape(-1, 3),
                          dirs.reshape(-1, 3),
                          mask=mask.reshape(-1),
                          geo_feat=geometric_features.view(
                              -1, geometric_features.shape[-1]),
                          sigma=sigma)
        rgbs = rgbs.view(N, -1, 3)  # [N, T+t, 3]

        #print(xyzs.shape, 'valid_rgb:', mask.sum().item())

        # calculate weight_sum (mask)
        weights[torch.logical_not(mask)] = 0.
        weights_sum = weights.sum(dim=-1)  # [N]

        # calculate depth
        depth = (weights * z_vals).sum(dim=-1)
        depth = depth / direction_norms

        depth_variance = (
            weights *
            (depth[..., None] - z_vals / direction_norms[..., None])**2).sum(
                dim=-1).detach()

        #calculate coordinates map
        weights = weights.unsqueeze(-1)
        coordinates_map = (weights * xyzs).sum(dim=-2)

        # calculate color
        image = torch.sum(weights * rgbs, dim=-2)  # [N, 3], in [0, 1]

        # mix background color
        if self.bg_radius > 0:
            assert (False)
            # use the bg model to calculate bg_color
            polar = raymarching.polar_from_ray(
                rays_o, rays_d, self.bg_radius)  # [N, 2] in [-1, 1]
            bg_color = self.background(polar, rays_d.reshape(-1, 3))  # [N, 3]
        elif bg_color is None:
            bg_color = 1

        image = image + (1 - weights_sum).unsqueeze(-1) * bg_color

        clip_features = self.clip(geometric_features.view(-1, geometric_features.shape[-1]), sigma)
        clip_features = clip_features.view(
                    (geometric_features.shape[0], geometric_features.shape[1],
                    clip_features.shape[-1]))
        clip_features = (weights.detach() * clip_features).sum(dim=-2)

        image = image.view(*prefix, 3)
        depth = depth.view(*prefix)
        coordinates_map = coordinates_map.view(*prefix, 3)
        clip_features = clip_features.view(*prefix, -1)
        
        # semantic, semantic_features = self.semantic(
        #     geometric_features.view(-1, geometric_features.shape[-1]), sigma)
        # semantic = semantic.view(
        #     (geometric_features.shape[0], geometric_features.shape[1],
        #      self.semantic_classes))
        # semantic_features = semantic_features.view(
        #     (geometric_features.shape[0], geometric_features.shape[1],
        #      semantic_features.shape[-1]))
        # semantic = (weights * semantic).sum(dim=-2)
        # semantic_features = (weights * semantic_features).sum(dim=-2)

        return {
            'depth': depth,
            'depth_variance': depth_variance,
            'image': image,
            # 'semantic': semantic,
            # 'semantic_features': semantic_features,
            'coordinates_map': coordinates_map,
            # Additional outputs needed for cluserting
            # 'weights': weights.squeeze(-1),
            'rgb': rgbs,
            'clip_features': clip_features
        }

    def run_cuda(self,
                 rays_o,
                 rays_d,
                 dt_gamma=0,
                 bg_color=None,
                 perturb=False,
                 force_all_rays=False,
                 max_steps=1024,
                 **kwargs):
        assert (False)
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # return: image: [B, N, 3], depth: [B, N]

        prefix = rays_o.shape[:-1]
        rays_o = rays_o.contiguous().view(-1, 3)
        rays_d = rays_d.contiguous().view(-1, 3)

        N = rays_o.shape[0]  # N = B * N, in fact
        device = rays_o.device

        # pre-calculate near far
        nears, fars = raymarching.near_far_from_aabb(
            rays_o, rays_d,
            self.aabb_train if self.training else self.aabb_infer,
            self.min_near)

        # mix background color
        if self.bg_radius > 0:
            # use the bg model to calculate bg_color
            polar = raymarching.polar_from_ray(
                rays_o, rays_d, self.bg_radius)  # [N, 2] in [-1, 1]
            bg_color = self.background(polar, rays_d)  # [N, 3]
        elif bg_color is None:
            bg_color = 1

        if self.training:
            # setup counter
            counter = self.step_counter[self.local_step % 16]
            counter.zero_()  # set to 0
            self.local_step += 1

            xyzs, dirs, deltas, rays = raymarching.march_rays_train(
                rays_o, rays_d, self.bound, self.density_bitfield, self.cascade,
                self.grid_size, nears, fars, counter, self.mean_count, perturb,
                128, force_all_rays, dt_gamma, max_steps)

            #plot_pointcloud(xyzs.reshape(-1, 3).detach().cpu().numpy())

            sigmas, rgbs, semantic = self(xyzs, dirs)
            # density_outputs = self.density(xyzs) # [M,], use a dict since it may include extra things, like geo_feat for rgb.
            # sigmas = density_outputs['sigma']
            # rgbs = self.color(xyzs, dirs, **density_outputs)
            sigmas = self.density_scale * sigmas

            #print(f'valid RGB query ratio: {mask.sum().item() / mask.shape[0]} (total = {mask.sum().item()})')

            # special case for CCNeRF's residual learning
            if len(sigmas.shape) == 2:
                K = sigmas.shape[0]
                depths = []
                images = []
                for k in range(K):
                    weights_sum, depth, image = raymarching.composite_rays_train(
                        sigmas[k], rgbs[k], deltas, rays)
                    image = image + (1 - weights_sum).unsqueeze(-1) * bg_color
                    depth = torch.clamp(depth - nears, min=0) / (fars - nears)
                    images.append(image.view(*prefix, 3))
                    depths.append(depth.view(*prefix))

                depth = torch.stack(depths, axis=0)  # [K, B, N]
                image = torch.stack(images, axis=0)  # [K, B, N, 3]

            else:

                weights_sum, depth, image = raymarching.composite_rays_train(
                    sigmas, rgbs, deltas, rays)
                image = image + (1 - weights_sum).unsqueeze(-1) * bg_color
                depth = torch.clamp(depth - nears, min=0) / (fars - nears)
                image = image.view(*prefix, 3)
                depth = depth.view(*prefix)

        else:

            # allocate outputs
            # if use autocast, must init as half so it won't be autocasted and lose reference.
            #dtype = torch.half if torch.is_autocast_enabled() else torch.float32
            # output should always be float32! only network inference uses half.
            dtype = torch.float32

            weights_sum = torch.zeros(N, dtype=dtype, device=device)
            depth = torch.zeros(N, dtype=dtype, device=device)
            image = torch.zeros(N, 3, dtype=dtype, device=device)

            n_alive = N
            alive_counter = torch.zeros([1], dtype=torch.int32, device=device)

            rays_alive = torch.zeros(2,
                                     n_alive,
                                     dtype=torch.int32,
                                     device=device)  # 2 is used to loop old/new
            rays_t = torch.zeros(2, n_alive, dtype=dtype, device=device)

            step = 0
            i = 0
            while step < max_steps:

                # count alive rays
                if step == 0:
                    # init rays at first step.
                    torch.arange(n_alive, out=rays_alive[0])
                    rays_t[0] = nears
                else:
                    alive_counter.zero_()
                    raymarching.compact_rays(n_alive, rays_alive[i % 2],
                                             rays_alive[(i + 1) % 2],
                                             rays_t[i % 2], rays_t[(i + 1) % 2],
                                             alive_counter)
                    n_alive = alive_counter.item()  # must invoke D2H copy here

                # exit loop
                if n_alive <= 0:
                    break

                # decide compact_steps
                n_step = max(min(N // n_alive, 8), 1)

                xyzs, dirs, deltas = raymarching.march_rays(
                    n_alive, n_step, rays_alive[i % 2], rays_t[i % 2], rays_o,
                    rays_d, self.bound, self.density_bitfield, self.cascade,
                    self.grid_size, nears, fars, 128, perturb, dt_gamma,
                    max_steps)

                sigmas, rgbs = self(xyzs, dirs)
                # density_outputs = self.density(xyzs) # [M,], use a dict since it may include extra things, like geo_feat for rgb.
                # sigmas = density_outputs['sigma']
                # rgbs = self.color(xyzs, dirs, **density_outputs)
                sigmas = self.density_scale * sigmas

                raymarching.composite_rays(n_alive, n_step, rays_alive[i % 2],
                                           rays_t[i % 2], sigmas, rgbs, deltas,
                                           weights_sum, depth, image)

                #print(f'step = {step}, n_step = {n_step}, n_alive = {n_alive}, xyzs: {xyzs.shape}')

                step += n_step
                i += 1

            image = image + (1 - weights_sum).unsqueeze(-1) * bg_color
            depth = torch.clamp(depth - nears, min=0) / (fars - nears)
            image = image.view(*prefix, 3)
            depth = depth.view(*prefix)

        return {
            'depth': depth,
            'image': image,
        }

    @torch.no_grad()
    def mark_untrained_grid(self, poses, intrinsic, S=64):
        # poses: [B, 4, 4]
        # intrinsic: [3, 3]

        if not self.cuda_ray:
            return

        if isinstance(poses, np.ndarray):
            poses = torch.from_numpy(poses)

        B = poses.shape[0]

        fx, fy, cx, cy = intrinsic

        X = torch.arange(self.grid_size,
                         dtype=torch.int32,
                         device=self.density_grid.device).split(S)
        Y = torch.arange(self.grid_size,
                         dtype=torch.int32,
                         device=self.density_grid.device).split(S)
        Z = torch.arange(self.grid_size,
                         dtype=torch.int32,
                         device=self.density_grid.device).split(S)

        count = torch.zeros_like(self.density_grid)
        poses = poses.to(count.device)

        # 5-level loop, forgive me...

        for xs in X:
            for ys in Y:
                for zs in Z:

                    # construct points
                    xx, yy, zz = custom_meshgrid(xs, ys, zs)
                    coords = torch.cat([
                        xx.reshape(-1, 1),
                        yy.reshape(-1, 1),
                        zz.reshape(-1, 1)
                    ],
                                       dim=-1)  # [N, 3], in [0, 128)
                    indices = raymarching.morton3D(coords).long()  # [N]
                    world_xyzs = (2 * coords.float() / (self.grid_size - 1) -
                                  1).unsqueeze(0)  # [1, N, 3] in [-1, 1]

                    # cascading
                    for cas in range(self.cascade):
                        bound = min(2**cas, self.bound)
                        half_grid_size = bound / self.grid_size
                        # scale to current cascade's resolution
                        cas_world_xyzs = world_xyzs * (bound - half_grid_size)

                        # split batch to avoid OOM
                        head = 0
                        while head < B:
                            tail = min(head + S, B)

                            # world2cam transform (poses is c2w, so we need to transpose it. Another transpose is needed for batched matmul, so the final form is without transpose.)
                            cam_xyzs = cas_world_xyzs - poses[head:tail, :3,
                                                              3].unsqueeze(1)
                            cam_xyzs = cam_xyzs @ poses[head:tail, :3, :
                                                        3]  # [S, N, 3]

                            # query if point is covered by any camera
                            mask_z = cam_xyzs[:, :, 2] > 0  # [S, N]
                            mask_x = torch.abs(
                                cam_xyzs[:, :, 0]
                            ) < cx / fx * cam_xyzs[:, :, 2] + half_grid_size * 2
                            mask_y = torch.abs(
                                cam_xyzs[:, :, 1]
                            ) < cy / fy * cam_xyzs[:, :, 2] + half_grid_size * 2
                            mask = (mask_z & mask_x & mask_y).sum(0).reshape(
                                -1)  # [N]

                            # update count
                            count[cas, indices] += mask
                            head += S

        # mark untrained grid as -1
        self.density_grid[count == 0] = -1

        #print(f'[mark untrained grid] {(count == 0).sum()} from {resolution ** 3 * self.cascade}')

    @torch.no_grad()
    def update_extra_state(self, decay=0.95, S=128):
        # call before each epoch to update extra states.

        if not self.cuda_ray:
            return

        ### update density grid

        tmp_grid = -torch.ones_like(self.density_grid)

        # full update.
        if self.iter_density < 16:
            #if True:
            X = torch.arange(self.grid_size,
                             dtype=torch.int32,
                             device=self.density_grid.device).split(S)
            Y = torch.arange(self.grid_size,
                             dtype=torch.int32,
                             device=self.density_grid.device).split(S)
            Z = torch.arange(self.grid_size,
                             dtype=torch.int32,
                             device=self.density_grid.device).split(S)

            for xs in X:
                for ys in Y:
                    for zs in Z:

                        # construct points
                        xx, yy, zz = custom_meshgrid(xs, ys, zs)
                        coords = torch.cat([
                            xx.reshape(-1, 1),
                            yy.reshape(-1, 1),
                            zz.reshape(-1, 1)
                        ],
                                           dim=-1)  # [N, 3], in [0, 128)
                        indices = raymarching.morton3D(coords).long()  # [N]
                        xyzs = 2 * coords.float() / (self.grid_size -
                                                     1) - 1  # [N, 3] in [-1, 1]

                        # cascading
                        for cas in range(self.cascade):
                            bound = min(2**cas, self.bound)
                            half_grid_size = bound / self.grid_size
                            # scale to current cascade's resolution
                            cas_xyzs = xyzs * (bound - half_grid_size)
                            # add noise in [-hgs, hgs]
                            cas_xyzs += (torch.rand_like(cas_xyzs) * 2 -
                                         1) * half_grid_size
                            # query density
                            sigmas = self.density(cas_xyzs)['sigma'].reshape(
                                -1).detach()
                            sigmas *= self.density_scale
                            # assign
                            tmp_grid[cas, indices] = sigmas

        # partial update (half the computation)
        # TODO: why no need of maxpool ?
        else:
            N = self.grid_size**3 // 4  # H * H * H / 4
            for cas in range(self.cascade):
                # random sample some positions
                coords = torch.randint(
                    0, self.grid_size, (N, 3),
                    device=self.density_grid.device)  # [N, 3], in [0, 128)
                indices = raymarching.morton3D(coords).long()  # [N]
                # random sample occupied positions
                occ_indices = torch.nonzero(self.density_grid[cas] > 0).squeeze(
                    -1)  # [Nz]
                rand_mask = torch.randint(0,
                                          occ_indices.shape[0], [N],
                                          dtype=torch.long,
                                          device=self.density_grid.device)
                occ_indices = occ_indices[
                    rand_mask]  # [Nz] --> [N], allow for duplication
                occ_coords = raymarching.morton3D_invert(occ_indices)  # [N, 3]
                # concat
                indices = torch.cat([indices, occ_indices], dim=0)
                coords = torch.cat([coords, occ_coords], dim=0)
                # same below
                xyzs = 2 * coords.float() / (self.grid_size -
                                             1) - 1  # [N, 3] in [-1, 1]
                bound = min(2**cas, self.bound)
                half_grid_size = bound / self.grid_size
                # scale to current cascade's resolution
                cas_xyzs = xyzs * (bound - half_grid_size)
                # add noise in [-hgs, hgs]
                cas_xyzs += (torch.rand_like(cas_xyzs) * 2 - 1) * half_grid_size
                # query density
                sigmas = self.density(cas_xyzs)['sigma'].reshape(-1).detach()
                sigmas *= self.density_scale
                # assign
                tmp_grid[cas, indices] = sigmas

        ## max-pool on tmp_grid for less aggressive culling [No significant improvement...]
        # invalid_mask = tmp_grid < 0
        # tmp_grid = F.max_pool3d(tmp_grid.view(self.cascade, 1, self.grid_size, self.grid_size, self.grid_size), kernel_size=3, stride=1, padding=1).view(self.cascade, -1)
        # tmp_grid[invalid_mask] = -1

        # ema update
        valid_mask = (self.density_grid >= 0) & (tmp_grid >= 0)
        self.density_grid[valid_mask] = torch.maximum(
            self.density_grid[valid_mask] * decay, tmp_grid[valid_mask])
        self.mean_density = torch.mean(self.density_grid.clamp(
            min=0)).item()  # -1 non-training regions are viewed as 0 density.
        self.iter_density += 1

        # convert to bitfield
        density_thresh = min(self.mean_density, self.density_thresh)
        self.density_bitfield = raymarching.packbits(self.density_grid,
                                                     density_thresh,
                                                     self.density_bitfield)

        ### update step counter
        total_step = min(16, self.local_step)
        if total_step > 0:
            self.mean_count = int(
                self.step_counter[:total_step, 0].sum().item() / total_step)
        self.local_step = 0

        #print(f'[density grid] min={self.density_grid.min().item():.4f}, max={self.density_grid.max().item():.4f}, mean={self.mean_density:.4f}, occ_rate={(self.density_grid > 0.01).sum() / (128**3 * self.cascade):.3f} | [step counter] mean={self.mean_count}')

    def render(self,
               rays_o,
               rays_d,
               direction_norms,
               staged=False,
               max_ray_batch=4096,
               **kwargs):
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # return: pred_rgb: [B, N, 3]

        if self.cuda_ray:
            _run = self.run_cuda
            assert (False)
        else:
            _run = self.run

        B, N = rays_o.shape[:2]
        device = rays_o.device

        # never stage when cuda_ray
        if staged and not self.cuda_ray:
            depth = torch.empty((B, N), device=device)
            depth_variance = torch.empty((B, N), device=device)
            image = torch.empty((B, N, 3), device=device)
            # semantic = torch.empty((B, N, self.semantic_classes), device=device)
            # semantic_features = torch.empty((B, N, self.hidden_dim_semantic),
            #                                 device=device)
            clip_features = torch.empty((B, N, self.clip_feat_dim),device=device)
            coordinates_map = torch.empty((B, N, 3), device=device)

            # additional feature from run
            # rgbs = torch.empty((B, N, N, 3), device=device)
            # weights = torch.empty((B, N, N), device=device)

            for b in range(B):
                head = 0
                while head < N:
                    tail = min(head + max_ray_batch, N)
                    results_ = _run(rays_o[b:b + 1, head:tail],
                                    rays_d[b:b + 1, head:tail],
                                    direction_norms[b:b + 1,
                                                    head:tail], **kwargs)
                    image[b:b + 1, head:tail] = results_['image']
                    depth[b:b + 1, head:tail] = results_['depth']
                    depth_variance[b:b + 1,
                                   head:tail] = results_['depth_variance']
                    # semantic[b:b + 1, head:tail, :] = results_['semantic']
                    # semantic_features[
                    #     b:b + 1, head:tail, :] = results_['semantic_features']
                    clip_features[
                        b:b + 1, head:tail, :] = results_['clip_features']
                    coordinates_map[b:b + 1,
                                    head:tail, :] = results_['coordinates_map']

                    # weights[b:b + 1, head:tail, head:tail] = results_['weights']
                    # rgbs[b:b + 1, head:tail, :] = results_['rgb']

                    head += max_ray_batch

            results = {}
            results['depth'] = depth
            results['depth_variance'] = depth_variance
            results['image'] = image
            # results['semantic'] = semantic
            # results['semantic_features'] = semantic_features
            results['clip_features'] = clip_features
            results['coordinates_map'] = coordinates_map
            # results['weights'] = weights
            # results['rgb'] = rgbs

        else:
            results = _run(rays_o, rays_d, direction_norms, **kwargs)

        return results
