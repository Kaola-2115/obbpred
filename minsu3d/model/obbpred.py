from pyexpat import features
import torch
import time
import torch.nn as nn
import numpy as np
import torchmetrics
import pytorch_lightning as pl
from minsu3d.evaluation.instance_segmentation import get_gt_instances, rle_encode
from minsu3d.evaluation.object_detection import get_gt_bbox
from minsu3d.common_ops.functions import hais_ops, common_ops
from minsu3d.optimizer import init_optimizer, cosine_lr_decay
from minsu3d.loss import MaskScoringLoss, ScoreLoss
from minsu3d.loss.utils import get_segmented_scores
from minsu3d.model.module import Backbone
from minsu3d.evaluation.semantic_segmentation import *
from minsu3d.model.general_model import GeneralModel, clusters_voxelization, get_batch_offsets


class ObbPred(pl.LightningModule):
    def __init__(self, model, data, optimizer, lr_decay, inference=None):
        super().__init__()
        self.save_hyperparameters()
        input_channel = model.use_coord * 3 + model.use_color * 3 + model.use_normal * 3 + model.use_multiview * 128
        self.backbone = Backbone(input_channel=input_channel,
                                 output_channel=model.m,
                                 block_channels=model.blocks,
                                 block_reps=model.block_reps,
                                 sem_classes=data.classes)
        if self.current_epoch > model.prepare_epochs and model.freeze_backbone:
            for param in self.backbone.parameters():
                param.requires_grad = False

    # def configure_optimizers(self):
    #     return init_optimizer(parameters=self.parameters(), **self.hparams.optimizer)

        # log hyperparameters
        
        self.conv1 = nn.Conv3d(16, 32, 2, 2, 2)
        self.conv2 = nn.Conv3d(32, 32, 2, 2, 2)
        self.conv3 = nn.Conv3d(4, 2, 3, 3, 3)
        self.conv4 = nn.Conv3d(2, 1, 3, 3, 3)

        self.relu = nn.functional.relu6

        self.pool1 = torch.nn.MaxPool3d(2)
        self.pool2 = torch.nn.MaxPool3d(2)
        
        n_sizes = self._get_conv_output()
        self.fc1 = nn.Linear(n_sizes, 512)
        self.fc2 = nn.Linear(512, 128)
        self.fc3 = nn.Linear(128, 2)
        self.accuracy = torchmetrics.Accuracy()
        self.log_softmax = nn.functional.log_softmax
        self.nll_loss = nn.functional.nll_loss
        self.learning_rate = 0.002


    # returns the size of the output tensor going into Linear layer from the conv block.
    def _get_conv_output(self):
        batch_size = 4
        input = torch.autograd.Variable(torch.rand(batch_size, 16, 40, 40, 40))
        output_feat = self._forward_features(input) 
        n_size = output_feat.data.view(batch_size, -1).size(1)
        return n_size
        
    # returns the feature tensor from the conv block
    def _forward_features(self, x):
        x = self.relu(self.conv1(x)).clone()
        x = self.pool1(self.relu(self.conv2(x))).clone()
        return x

    def _forward_voxelize(self, data_dict):
        #output data
        if self.hparams.model.use_coord:
            data_dict["feats"] = torch.cat((data_dict["feats"], data_dict["locs"]), dim=1)
        data_dict["voxel_feats"] = common_ops.voxelization(data_dict["feats"].to(torch.float32), data_dict["p2v_map"].to(torch.int)) # (M, C), float, cuda
        backbone_output_dict = self.backbone(data_dict["voxel_feats"], data_dict["voxel_locs"], data_dict["v2p_map"])
        divided_feature = []
        features = backbone_output_dict["point_features"]
        # features_set = []
        # if len(data_dict["batch_divide"]) == 4:
        #     for i in range(3):
        #         data_dict["batch_divide"][i+1] = data_dict["batch_divide"][i+1].clone() + data_dict["batch_divide"][i].clone()
        #     features_set.append(features[:(data_dict["batch_divide"][0].item())])
        #     features_set.append(features[(data_dict["batch_divide"][0].item()):(data_dict["batch_divide"][1].item())])
        #     features_set.append(features[(data_dict["batch_divide"][1].item()):(data_dict["batch_divide"][2].item())])
        #     features_set.append(features[(data_dict["batch_divide"][2].item()):(data_dict["batch_divide"][3].item())])
        #     density = torch.zeros([4, 40, 40, 40], dtype=torch.float).cuda()
        #     downsample_feat = torch.zeros([4, 16, 40, 40, 40], dtype=torch.float).cuda()
        #     for i in range(4):
        #         if i == 0:
        #             xyz_i = data_dict["locs"][:(data_dict["batch_divide"][0].item())]
        #         else:
        #             xyz_i = data_dict["locs"][(data_dict["batch_divide"][i-1].item()):(data_dict["batch_divide"][i].item())]
        #         xyz_x = xyz_i[:,0]
        #         xyz_y = xyz_i[:,1]
        #         xyz_z = xyz_i[:,2]
        #         x_min = xyz_x.min()
        #         y_min = xyz_y.min()
        #         z_min = xyz_z.min()
        #         x_max = xyz_x.max()
        #         y_max = xyz_y.max()
        #         z_max = xyz_z.max()
        #         id = 0
        #         for x, y, z in xyz_i:
        #             x_grid = (39.0*(x-x_min)/(x_max-x_min)).to(torch.int)
        #             y_grid = (39.0*(y-y_min)/(y_max-y_min)).to(torch.int)
        #             z_grid = (39.0*(z-z_min)/(z_max-z_min)).to(torch.int)
        #             density[i][x_grid][y_grid][z_grid] = density[i][x_grid][y_grid][z_grid] + 1
        #             downsample_feat[i,:,x_grid,y_grid,z_grid] = features_set[i][id]
        #             id = id + 1
        #         for x in range(40):
        #             for y in range(40):
        #                 for z in range(40):
        #                     if density[i][x][y][z] != 0:
        #                         downsample_feat[i,:,x,y,z] = downsample_feat[i,:,x,y,z].clone()/density[i,x,y,z]
        #     return downsample_feat
        # else:
        density = torch.zeros([40, 40, 40], dtype=torch.float).cuda()
        downsample_feat = torch.zeros([16, 40, 40, 40], dtype=torch.float).cuda()
        xyz_i = data_dict["locs"]
        xyz_x = xyz_i[:,0]
        xyz_y = xyz_i[:,1]
        xyz_z = xyz_i[:,2]
        x_min = xyz_x.min()
        y_min = xyz_y.min()
        z_min = xyz_z.min()
        x_max = xyz_x.max()
        y_max = xyz_y.max()
        z_max = xyz_z.max()
        id = 0
        for x, y, z in xyz_i:
            x_grid = (39.0*(x-x_min)/(x_max-x_min)).to(torch.int)
            y_grid = (39.0*(y-y_min)/(y_max-y_min)).to(torch.int)
            z_grid = (39.0*(z-z_min)/(z_max-z_min)).to(torch.int)
            density[x_grid][y_grid][z_grid] = density[x_grid][y_grid][z_grid] + 1
            downsample_feat[:,x_grid,y_grid,z_grid] = features[id]
            id = id + 1
        for x in range(40):
            for y in range(40):
                for z in range(40):
                    if density[x][y][z] != 0:
                        downsample_feat[:,x,y,z] = downsample_feat[:,x,y,z].clone()/density[x,y,z]
        if len(data_dict["batch_divide"]) == 4:
            return downsample_feat[None,:].repeat(4,1,1,1,1)
        else:
            return downsample_feat[None,:]

    # will be used during inference
    def forward(self, data_dict):
        x = self._forward_voxelize(data_dict)
        x = self._forward_features(x)
        x = x.view(x.size(0), -1)
        x = self.relu(self.fc1(x)).clone()
        x = self.relu(self.fc2(x)).clone()
        x = self.log_softmax(self.fc3(x))
        
        return x

    def training_step(self, data_dict, idx):
        y = self.forward(data_dict)
        loss = self.nll_loss(y, data_dict["class"])
        self.log("train/loss", loss, prog_bar=True, on_step=False, on_epoch=True,
                 sync_dist=True, batch_size=self.hparams.data.batch_size)
        # training metrics
        preds = torch.argmax(y, dim=1)
        acc = self.accuracy(preds, data_dict["class"])
        self.log('train_loss', loss, on_step=True, on_epoch=True, logger=True)
        self.log('train_acc', acc, on_step=True, on_epoch=True, logger=True)
        
        return loss

    def validation_step(self, data_dict, idx):
        y = self.forward(data_dict)
        loss = self.nll_loss(y, data_dict["class"])
        
        # validation metrics
        preds = torch.argmax(y, dim=1)
        acc = self.accuracy(preds, data_dict["class"])
        self.log('val_loss', loss, prog_bar=True)
        self.log('val_acc', acc, prog_bar=True)
        return loss

    def test_step(self, data_dict, idx):
        y = self.forward(data_dict)
        loss = self.nll_loss(y, data_dict["class"])
        
        # validation metrics
        preds = torch.argmax(y, dim=1)
        acc = self.accuracy(preds, data_dict["class"])
        self.log('test_loss', loss, prog_bar=True)
        self.log('test_acc', acc, prog_bar=True)
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        return optimizer

