from torch.utils.data import Dataset

import numpy as np
import os
from PIL import Image
import cv2
import torch
from lib.graphics_utils import getWorld2View2, getProjectionMatrix, focal2fov
from pathlib import Path
import logging
import json
from tqdm import tqdm


def save_np_to_json(parm, save_name):
    for key in parm.keys():
        parm[key] = parm[key].tolist()
    with open(save_name, 'w') as file:
        json.dump(parm, file, indent=1)


def load_json_to_np(parm_name):
    with open(parm_name, 'r') as f:
        parm = json.load(f)
    for key in parm.keys():
        parm[key] = np.array(parm[key])
    return parm


def depth2pts(depth, extrinsic, intrinsic):
    # depth H W extrinsic 3x4 intrinsic 3x3 pts map H W 3
    rot = extrinsic[:3, :3]
    trans = extrinsic[:3, 3:]
    S, S = depth.shape

    y, x = torch.meshgrid(torch.linspace(0.5, S-0.5, S, device=depth.device),
                          torch.linspace(0.5, S-0.5, S, device=depth.device))
    pts_2d = torch.stack([x, y, torch.ones_like(x)], dim=-1)  # H W 3

    pts_2d[..., 2] = 1.0 / (depth + 1e-8)
    pts_2d[..., 0] -= intrinsic[0, 2]
    pts_2d[..., 1] -= intrinsic[1, 2]
    pts_2d_xy = pts_2d[..., :2] * pts_2d[..., 2:]
    pts_2d = torch.cat([pts_2d_xy, pts_2d[..., 2:]], dim=-1)

    pts_2d[..., 0] /= intrinsic[0, 0]
    pts_2d[..., 1] /= intrinsic[1, 1]
    pts_2d = pts_2d.reshape(-1, 3).T
    pts = rot.T @ pts_2d - rot.T @ trans
    return pts.T.view(S, S, 3)


def pts2depth(ptsmap, extrinsic, intrinsic):
    S, S, _ = ptsmap.shape
    pts = ptsmap.view(-1, 3).T
    calib = intrinsic @ extrinsic
    pts = calib[:3, :3] @ pts
    pts = pts + calib[:3, 3:4]
    pts[:2, :] /= (pts[2:, :] + 1e-8)
    depth = 1.0 / (pts[2, :].view(S, S) + 1e-8)
    return depth


def stereo_pts2flow(pts0, pts1, rectify0, rectify1, Tf_x):
    new_extr0, new_intr0, rectify_mat0_x, rectify_mat0_y = rectify0
    new_extr1, new_intr1, rectify_mat1_x, rectify_mat1_y = rectify1
    new_depth0 = pts2depth(torch.FloatTensor(pts0), torch.FloatTensor(new_extr0), torch.FloatTensor(new_intr0))
    new_depth1 = pts2depth(torch.FloatTensor(pts1), torch.FloatTensor(new_extr1), torch.FloatTensor(new_intr1))
    new_depth0 = new_depth0.detach().numpy()
    new_depth1 = new_depth1.detach().numpy()
    new_depth0 = cv2.remap(new_depth0, rectify_mat0_x, rectify_mat0_y, cv2.INTER_LINEAR)
    new_depth1 = cv2.remap(new_depth1, rectify_mat1_x, rectify_mat1_y, cv2.INTER_LINEAR)

    offset0 = new_intr1[0, 2] - new_intr0[0, 2]
    disparity0 = -new_depth0 * Tf_x
    flow0 = offset0 - disparity0

    offset1 = new_intr0[0, 2] - new_intr1[0, 2]
    disparity1 = -new_depth1 * (-Tf_x)
    flow1 = offset1 - disparity1

    flow0[new_depth0 < 0.05] = 0
    flow1[new_depth1 < 0.05] = 0

    return flow0, flow1


def read_img(name):
    img = np.array(Image.open(name))
    return img


def read_depth(name):
    return cv2.imread(name, cv2.IMREAD_UNCHANGED).astype(np.float32) / 2.0 ** 15


class StereoHumanDataset(Dataset):
    def __init__(self, opt, phase='train'):
        self.opt = opt
        self.use_processed_data = opt.use_processed_data
        self.phase = phase
        if self.phase == 'train':
            self.data_root = os.path.join(opt.data_root, 'train')
        elif self.phase == 'val':
            self.data_root = os.path.join(opt.data_root, 'val')
        elif self.phase == 'test':
            self.data_root = opt.test_data_root

        self.img_path = os.path.join(self.data_root, 'img/%s/%d.jpg')
        self.img_hr_path = os.path.join(self.data_root, 'img/%s/%d_hr.jpg')
        self.mask_path = os.path.join(self.data_root, 'mask/%s/%d.png')
        self.depth_path = os.path.join(self.data_root, 'depth/%s/%d.png')
        self.intr_path = os.path.join(self.data_root, 'parm/%s/%d_intrinsic.npy')
        self.extr_path = os.path.join(self.data_root, 'parm/%s/%d_extrinsic.npy')
        self.sample_list = sorted(list(os.listdir(os.path.join(self.data_root, 'img'))))
        self.heatmap_path = os.path.join(self.data_root, 'heatmap/%s/%d.png')

        if self.use_processed_data:
            self.local_data_root = os.path.join(opt.data_root, 'rectified_local', self.phase)
            self.local_img_path = os.path.join(self.local_data_root, 'img/%s/%d.jpg')
            self.local_mask_path = os.path.join(self.local_data_root, 'mask/%s/%d.png')
            self.local_flow_path = os.path.join(self.local_data_root, 'flow/%s/%d.npy')
            self.local_valid_path = os.path.join(self.local_data_root, 'valid/%s/%d.png')
            self.local_parm_path = os.path.join(self.local_data_root, 'parm/%s/%d_%d.json')
            self.local_heatmap_path = os.path.join(self.local_data_root, 'heatmap/%s/%d.png')

            if os.path.exists(self.local_data_root):
                assert len(os.listdir(os.path.join(self.local_data_root, 'img'))) == len(self.sample_list)
                logging.info(f"Using local data in {self.local_data_root} ...")
            else:
                self.save_local_stereo_data()

    def save_local_stereo_data(self):
        logging.info(f"Generating data to {self.local_data_root} ...")
        for sample_name in tqdm(self.sample_list):
            view0_data = self.load_single_view(
                sample_name, self.opt.source_id[0], hr_img=False,
                require_mask=True, require_pts=True
            )
            view1_data = self.load_single_view(
                sample_name, self.opt.source_id[1], hr_img=False,
                require_mask=True, require_pts=True
            )
            lmain_stereo_np = self.get_rectified_stereo_data(
                main_view_data=view0_data, ref_view_data=view1_data
            )

            # Create directories for each data type
            for sub_dir in ['/img/', '/mask/', '/flow/', '/valid/', '/parm/', '/heatmap/']:
                Path(self.local_data_root + sub_dir + str(sample_name)).mkdir(exist_ok=True, parents=True)

            # Construct save paths
            img0_save_name = self.local_img_path % (sample_name, self.opt.source_id[0])
            mask0_save_name = self.local_mask_path % (sample_name, self.opt.source_id[0])
            img1_save_name = self.local_img_path % (sample_name, self.opt.source_id[1])
            mask1_save_name = self.local_mask_path % (sample_name, self.opt.source_id[1])
            flow0_save_name = self.local_flow_path % (sample_name, self.opt.source_id[0])
            valid0_save_name = self.local_valid_path % (sample_name, self.opt.source_id[0])
            flow1_save_name = self.local_flow_path % (sample_name, self.opt.source_id[1])
            valid1_save_name = self.local_valid_path % (sample_name, self.opt.source_id[1])
            parm_save_name = self.local_parm_path % (sample_name, self.opt.source_id[0], self.opt.source_id[1])
            heatmap0_save_name = self.local_heatmap_path % (sample_name, self.opt.source_id[0])
            heatmap1_save_name = self.local_heatmap_path % (sample_name, self.opt.source_id[1])

            # Save images and masks
            Image.fromarray(lmain_stereo_np['img0']).save(img0_save_name, quality=95)
            Image.fromarray(lmain_stereo_np['mask0']).save(mask0_save_name)
            Image.fromarray(lmain_stereo_np['img1']).save(img1_save_name, quality=95)
            Image.fromarray(lmain_stereo_np['mask1']).save(mask1_save_name)

            # Save optical flow and valid masks
            np.save(flow0_save_name, lmain_stereo_np['flow0'].astype(np.float16))
            Image.fromarray(lmain_stereo_np['valid0']).save(valid0_save_name)
            np.save(flow1_save_name, lmain_stereo_np['flow1'].astype(np.float16))
            Image.fromarray(lmain_stereo_np['valid1']).save(valid1_save_name)

            # Save camera parameters
            save_np_to_json(lmain_stereo_np['camera'], parm_save_name)

            # Save heatmaps
            heatmap0 = read_img(self.heatmap_path % (sample_name, self.opt.source_id[0]))
            heatmap1 = read_img(self.heatmap_path % (sample_name, self.opt.source_id[1]))
            Image.fromarray(heatmap0).save(heatmap0_save_name)
            Image.fromarray(heatmap1).save(heatmap1_save_name)

        logging.info("Generating data Done!")

    def load_local_stereo_data(self, sample_name):
        img0_name = self.local_img_path % (sample_name, self.opt.source_id[0])
        mask0_name = self.local_mask_path % (sample_name, self.opt.source_id[0])
        heatmap0_name = self.local_heatmap_path % (sample_name, self.opt.source_id[0])

        img1_name = self.local_img_path % (sample_name, self.opt.source_id[1])
        mask1_name = self.local_mask_path % (sample_name, self.opt.source_id[1])
        heatmap1_name = self.local_heatmap_path % (sample_name, self.opt.source_id[1])

        flow0_name = self.local_flow_path % (sample_name, self.opt.source_id[0])
        flow1_name = self.local_flow_path % (sample_name, self.opt.source_id[1])
        valid0_name = self.local_valid_path % (sample_name, self.opt.source_id[0])
        valid1_name = self.local_valid_path % (sample_name, self.opt.source_id[1])
        parm_name = self.local_parm_path % (sample_name, self.opt.source_id[0], self.opt.source_id[1])

        stereo_data = {
            'img0': read_img(img0_name),
            'mask0': read_img(mask0_name),
            'heatmap0': read_img(heatmap0_name),
            'img1': read_img(img1_name),
            'mask1': read_img(mask1_name),
            'heatmap1': read_img(heatmap1_name),
            'camera': load_json_to_np(parm_name),
            'flow0': np.load(flow0_name),
            'valid0': read_img(valid0_name),
            'flow1': np.load(flow1_name),
            'valid1': read_img(valid1_name)
        }

        return stereo_data

    def load_single_view(self, sample_name, source_id, hr_img=False, require_mask=True, require_pts=True, require_heatmap=False):
        img_name = self.img_path % (sample_name, source_id)
        image_hr_name = self.img_hr_path % (sample_name, source_id)
        mask_name = self.mask_path % (sample_name, source_id)
        depth_name = self.depth_path % (sample_name, source_id)
        intr_name = self.intr_path % (sample_name, source_id)
        extr_name = self.extr_path % (sample_name, source_id)
        heatmap_name = self.heatmap_path % (sample_name, source_id)

        intr, extr = np.load(intr_name), np.load(extr_name)
        mask, pts, heatmap = None, None, None

        if hr_img:
            img = read_img(image_hr_name)
            intr[:2] *= 2
        else:
            img = read_img(img_name)

        if require_mask:
            mask = read_img(mask_name)

        if require_pts and os.path.exists(depth_name):
            depth = read_depth(depth_name)
            pts = depth2pts(torch.FloatTensor(depth), torch.FloatTensor(extr), torch.FloatTensor(intr))

        if require_heatmap and os.path.exists(heatmap_name):
            heatmap = read_img(heatmap_name)

        return img, mask, intr, extr, pts, heatmap

    def get_novel_view_tensor(self, sample_name, view_id):
        img, _, intr, extr, _, _ = self.load_single_view(sample_name, view_id, hr_img=self.opt.use_hr_img,
                                                      require_mask=False, require_pts=False)
        height, width = img.shape[:2]
        img = torch.from_numpy(img).permute(2, 0, 1)
        img = img / 255.0

        R = np.array(extr[:3, :3], np.float32).reshape(3, 3).transpose(1, 0)
        T = np.array(extr[:3, 3], np.float32)

        FovX = focal2fov(intr[0, 0], width)
        FovY = focal2fov(intr[1, 1], height)
        projection_matrix = getProjectionMatrix(znear=self.opt.znear, zfar=self.opt.zfar, K=intr, h=height, w=width).transpose(0, 1)
        world_view_transform = torch.tensor(getWorld2View2(R, T, np.array(self.opt.trans), self.opt.scale)).transpose(0, 1)
        full_proj_transform = (world_view_transform.unsqueeze(0).bmm(projection_matrix.unsqueeze(0))).squeeze(0)
        camera_center = world_view_transform.inverse()[3, :3]

        novel_view_data = {
            'view_id': torch.IntTensor([view_id]),
            'img': img,
            'extr': torch.FloatTensor(extr),
            'FovX': FovX,
            'FovY': FovY,
            'width': width,
            'height': height,
            'world_view_transform': world_view_transform,
            'full_proj_transform': full_proj_transform,
            'camera_center': camera_center
        }

        return novel_view_data

    def get_rectified_stereo_data(self, main_view_data, ref_view_data):
        img0, mask0, intr0, extr0, pts0, heatmap0 = main_view_data
        img1, mask1, intr1, extr1, pts1, heatmap1 = ref_view_data  # Assuming heatmaps are passed here as well

        H, W = self.opt.src_res, self.opt.src_res
        r0, t0 = extr0[:3, :3], extr0[:3, 3:]
        r1, t1 = extr1[:3, :3], extr1[:3, 3:]
        inv_r0 = r0.T
        inv_t0 = - r0.T @ t0
        E0 = np.eye(4)
        E0[:3, :3], E0[:3, 3:] = inv_r0, inv_t0
        E1 = np.eye(4)
        E1[:3, :3], E1[:3, 3:] = r1, t1
        E = E1 @ E0
        R, T = E[:3, :3], E[:3, 3]
        dist0, dist1 = np.zeros(4), np.zeros(4)

        R0, R1, P0, P1, _, _, _ = cv2.stereoRectify(intr0, dist0, intr1, dist1, (W, H), R, T, flags=0)

        new_extr0 = R0 @ extr0
        new_intr0 = P0[:3, :3]
        new_extr1 = R1 @ extr1
        new_intr1 = P1[:3, :3]
        Tf_x = np.array(P1[0, 3])

        camera = {
            'intr0': new_intr0,
            'intr1': new_intr1,
            'extr0': new_extr0,
            'extr1': new_extr1,
            'Tf_x': Tf_x
        }

        rectify_mat0_x, rectify_mat0_y = cv2.initUndistortRectifyMap(intr0, dist0, R0, P0, (W, H), cv2.CV_32FC1)
        new_img0 = cv2.remap(img0, rectify_mat0_x, rectify_mat0_y, cv2.INTER_LINEAR)
        new_mask0 = cv2.remap(mask0, rectify_mat0_x, rectify_mat0_y, cv2.INTER_LINEAR)
        rectify_mat1_x, rectify_mat1_y = cv2.initUndistortRectifyMap(intr1, dist1, R1, P1, (W, H), cv2.CV_32FC1)
        new_img1 = cv2.remap(img1, rectify_mat1_x, rectify_mat1_y, cv2.INTER_LINEAR)
        new_mask1 = cv2.remap(mask1, rectify_mat1_x, rectify_mat1_y, cv2.INTER_LINEAR)
        rectify0 = new_extr0, new_intr0, rectify_mat0_x, rectify_mat0_y
        rectify1 = new_extr1, new_intr1, rectify_mat1_x, rectify_mat1_y

        new_heatmap0 = cv2.remap(heatmap0, rectify_mat0_x, rectify_mat0_y, cv2.INTER_LINEAR)
        new_heatmap1 = cv2.remap(heatmap1, rectify_mat1_x, rectify_mat1_y, cv2.INTER_LINEAR)

        stereo_data = {
            'img0': new_img0,
            'mask0': new_mask0,
            'img1': new_img1,
            'mask1': new_mask1,
            'camera': camera,
            'heatmap0': new_heatmap0,  # Add heatmap for the main view
            'heatmap1': new_heatmap1   # Add heatmap for the reference view
        }

        # for key, value in stereo_data.items():
        #     print(f"The shape of {key} is: {value.shape}")

        if pts0 is not None:
            flow0, flow1 = stereo_pts2flow(pts0, pts1, rectify0, rectify1, Tf_x)

            kernel = np.ones((3, 3), dtype=np.uint8)
            flow_eroded, valid_eroded = [], []
            
            for (flow, new_mask, heatmap) in [(flow0, new_mask0, heatmap0), (flow1, new_mask1, heatmap1)]:
                valid = (new_mask.copy()[:, :, 0] / 255.0).astype(np.float32)
                valid = cv2.erode(valid, kernel, 1)
                valid[valid >= 0.66] = 1.0
                valid[valid < 0.66] = 0.0
                
                # Convert heatmap (3 channels) to grayscale (single channel)
                heatmap_gray = np.mean(heatmap, axis=-1)  # Shape (1024, 1024)

                # Ensure that both flow and heatmap_gray are of float32 type
                flow = flow.astype(np.float32)
                heatmap_gray = heatmap_gray.astype(np.float32)
                
                # Use valid mask and heatmap to adjust flow and valid mask
                flow *= valid
                valid *= 255.0
                flow *= heatmap_gray  # Use heatmap to scale/modify flow (0 = low confidence, 1 = high confidence)
                valid *= heatmap_gray  # Use heatmap to modify valid areas
                
                flow_eroded.append(flow)
                valid_eroded.append(valid)

            stereo_data.update({
                'flow0': flow_eroded[0],
                'valid0': valid_eroded[0].astype(np.uint8),
                'flow1': flow_eroded[1],
                'valid1': valid_eroded[1].astype(np.uint8)
            })

        return stereo_data


    def stereo_to_dict_tensor(self, stereo_data, subject_name):
        img_tensor, mask_tensor, heatmap_tensor = [], [], []
        
        for (img_view, mask_view, heatmap_view) in [('img0', 'mask0', 'heatmap0'), ('img1', 'mask1', 'heatmap1')]:
            # Convert images to tensors
            img = torch.from_numpy(stereo_data[img_view]).permute(2, 0, 1)
            img = 2 * (img / 255.0) - 1.0
            # Convert masks to tensors and normalize
            mask = torch.from_numpy(stereo_data[mask_view]).permute(2, 0, 1).float()
            mask = mask / 255.0
            
            # Convert heatmaps to tensors and normalize (Assuming heatmaps are available)
            heatmap = torch.from_numpy(stereo_data[heatmap_view]).permute(2, 0, 1).float()
            heatmap = heatmap / 255.0  # Normalize heatmap to [0, 1] range, if it's in the range [0, 255]
            # Apply masks to images and scale the heatmap
            img = img * mask
            mask[mask < 0.5] = 0.0
            mask[mask >= 0.5] = 1.0

            # Collect tensors for image, mask, and heatmap
            img_tensor.append(img)
            mask_tensor.append(mask)
            heatmap_tensor.append(heatmap)

        # Organize main and reference data
        lmain_data = {
            'img': img_tensor[0],
            'mask': mask_tensor[0],
            'heatmap': heatmap_tensor[0],  # Add heatmap for main view
            'intr': torch.FloatTensor(stereo_data['camera']['intr0']),
            'ref_intr': torch.FloatTensor(stereo_data['camera']['intr1']),
            'extr': torch.FloatTensor(stereo_data['camera']['extr0']),
            'Tf_x': torch.FloatTensor(stereo_data['camera']['Tf_x'])
        }

        rmain_data = {
            'img': img_tensor[1],
            'mask': mask_tensor[1],
            'heatmap': heatmap_tensor[1],  # Add heatmap for reference view
            'intr': torch.FloatTensor(stereo_data['camera']['intr1']),
            'ref_intr': torch.FloatTensor(stereo_data['camera']['intr0']),
            'extr': torch.FloatTensor(stereo_data['camera']['extr1']),
            'Tf_x': -torch.FloatTensor(stereo_data['camera']['Tf_x'])
        }

        # Handle flow and validity data if available
        if 'flow0' in stereo_data:
            flow_tensor, valid_tensor = [], []
            for (flow_view, valid_view) in [('flow0', 'valid0'), ('flow1', 'valid1')]:
                flow = torch.from_numpy(stereo_data[flow_view])
                flow = torch.unsqueeze(flow, dim=0)
                flow_tensor.append(flow)

                valid = torch.from_numpy(stereo_data[valid_view])
                valid = torch.unsqueeze(valid, dim=0)
                valid = valid / 255.0
                valid_tensor.append(valid)

            lmain_data['flow'], lmain_data['valid'] = flow_tensor[0], valid_tensor[0]
            rmain_data['flow'], rmain_data['valid'] = flow_tensor[1], valid_tensor[1]

        # Return the dictionary containing the data
        return {'name': subject_name, 'lmain': lmain_data, 'rmain': rmain_data}

    def get_item(self, index, novel_id=None):
        sample_id = index % len(self.sample_list)
        sample_name = self.sample_list[sample_id]

        if self.use_processed_data:
            stereo_np = self.load_local_stereo_data(sample_name)
        else:
            # Load view0 and view1 data with masks and heatmaps
            view0_data = self.load_single_view(sample_name, self.opt.source_id[0], hr_img=False,
                                            require_mask=True, require_pts=True, require_heatmap=True)
            view1_data = self.load_single_view(sample_name, self.opt.source_id[1], hr_img=False,
                                            require_mask=True, require_pts=True, require_heatmap=True)
            
            # The view data includes image, mask, and heatmap
            img0, mask0, heatmap0 = view0_data[0], view0_data[1], view0_data[2]  # Extract image, mask, and heatmap for view0
            img1, mask1, heatmap1 = view1_data[0], view1_data[1], view1_data[2]  # Extract image, mask, and heatmap for view1

            stereo_np = self.get_rectified_stereo_data(main_view_data=view0_data, ref_view_data=view1_data)

        # Convert stereo data to dict tensor
        dict_tensor = self.stereo_to_dict_tensor(stereo_np, sample_name)

        # Handle novel view data if provided
        if novel_id:
            novel_id = np.random.choice(novel_id)
            dict_tensor.update({
                'novel_view': self.get_novel_view_tensor(sample_name, novel_id)
            })

        return dict_tensor

    def get_test_item(self, index, source_id):
        sample_id = index % len(self.sample_list)
        sample_name = self.sample_list[sample_id]

        if self.use_processed_data:
            logging.error('test data loader not support processed data')

        # Load the left and right view data along with their masks and heatmaps
        view0_data = self.load_single_view(sample_name, source_id[0], hr_img=False, require_mask=True, require_pts=False, require_heatmap=True)
        view1_data = self.load_single_view(sample_name, source_id[1], hr_img=False, require_mask=True, require_pts=False, require_heatmap=True)

        # Extract the images, masks, and heatmaps
        img0, mask0, heatmap0 = view0_data[0], view0_data[1], view0_data[2]  # img0 is image, mask0 is the mask, heatmap0 is the heatmap
        img1, mask1, heatmap1 = view1_data[0], view1_data[1], view1_data[2]  # img1 is image, mask1 is the mask, heatmap1 is the heatmap
        # Generate rectified stereo data
        stereo_np = self.get_rectified_stereo_data(main_view_data=view0_data, ref_view_data=view1_data)
        dict_tensor = self.stereo_to_dict_tensor(stereo_np, sample_name)

        # Add original intrinsics and extrinsics
        lmain_intr_ori, lmain_extr_ori = view0_data[2], view0_data[3]
        rmain_intr_ori, rmain_extr_ori = view1_data[2], view1_data[3]
        
        dict_tensor['lmain']['intr_ori'] = torch.FloatTensor(lmain_intr_ori)
        dict_tensor['rmain']['intr_ori'] = torch.FloatTensor(rmain_intr_ori)
        dict_tensor['lmain']['extr_ori'] = torch.FloatTensor(lmain_extr_ori)
        dict_tensor['rmain']['extr_ori'] = torch.FloatTensor(rmain_extr_ori)

        # Image size calculation
        img_len = self.opt.src_res * 2 if self.opt.use_hr_img else self.opt.src_res
        novel_dict = {
            'height': torch.IntTensor([img_len]),
            'width': torch.IntTensor([img_len])
        }

        dict_tensor.update({
            'novel_view': novel_dict
        })

        return dict_tensor


    def __getitem__(self, index):
        if self.phase == 'train':
            return self.get_item(index, novel_id=self.opt.train_novel_id)
        elif self.phase == 'val':
            return self.get_item(index, novel_id=self.opt.val_novel_id)

    def __len__(self):
        self.train_boost = 50
        self.val_boost = 200
        if self.phase == 'train':
            return len(self.sample_list) * self.train_boost
        elif self.phase == 'val':
            return len(self.sample_list) * self.val_boost
        else:
            return len(self.sample_list)
