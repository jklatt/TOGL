import argparse
import math
import torch
import torch.nn as nn
import torch.nn.functional as F

import pytorch_lightning as pl

from torch_geometric.nn import GCNConv, GINConv, global_mean_pool, global_add_pool

from torch_geometric.data import DataLoader, Batch, Data

from topognn import Tasks
from topognn.cli_utils import str2bool
from topognn.topo_utils import batch_persistence_routine, persistence_routine, parallel_persistence_routine
from topognn.layers import GCNLayer, GINLayer
from topognn.metrics import WeightedAccuracy
from torch_persistent_homology.persistent_homology_cpu import compute_persistence_homology_batched_mt

import topognn.coord_transforms as coord_transforms
import numpy as np


class TopologyLayer(torch.nn.Module):
    """Topological Aggregation Layer."""

    def __init__(self, features_in, features_out, num_filtrations, num_coord_funs, filtration_hidden, num_coord_funs1=None, dim1=False, set2set=False, set_out_dim=32):
        """
        num_coord_funs is a dictionary with the numbers of coordinate functions of each type.
        dim1 is a boolean. True if we have to return dim1 persistence.
        """
        super().__init__()

        self.dim1 = dim1

        self.features_in = features_in
        self.features_out = features_out

        self.num_filtrations = num_filtrations
        self.num_coord_funs = num_coord_funs

        self.filtration_hidden = filtration_hidden

        self.total_num_coord_funs = np.array(
            list(num_coord_funs.values())).sum()

        self.coord_fun_modules = torch.nn.ModuleList([
            getattr(coord_transforms, key)(output_dim=num_coord_funs[key])
            for key in num_coord_funs
        ])

        self.set2set = set2set
        if self.set2set:
            # NB if we want to have a single set transformer for all filtrations, the dim_in should be 2*num_filtrations.

            self.set_transformer = coord_transforms.Set2SetMod(dim_in=2, dim_out=set_out_dim,
                                                               num_heads=4, num_inds=256)

            # self.set_transformer = coord_transforms.ISAB(dim_in = 2 ,
            #                                         dim_out = set_out_dim,
            #                                         num_heads = 4,
            #                                         num_inds = 256)

            if self.dim1:
                self.set_transformer1 = coord_transforms.Set2SetMod(dim_in=2, dim_out=set_out_dim,
                                                                    num_heads=4, num_inds=256)

                # coord_transforms.ISAB(dim_in = 2 ,
                #                                     dim_out = set_out_dim,
                #                                     num_heads = 4,
                #                                     num_inds = 256)

        if self.dim1:
            assert num_coord_funs1 is not None
            self.coord_fun_modules1 = torch.nn.ModuleList([
                getattr(coord_transforms, key)(output_dim=num_coord_funs1[key])
                for key in num_coord_funs1
            ])

        # TODO : do we want to do weight sharing ? e.g. one big NN with num_filtrations outputs.
        self.filtration_modules = torch.nn.ModuleList([

            torch.nn.Sequential(
                torch.nn.Linear(self.features_in, self.filtration_hidden),
                torch.nn.ReLU(),
                torch.nn.Linear(self.filtration_hidden, 1)) for _ in range(num_filtrations)
        ])

        if self.set2set:
            in_out_dim = self.features_in + set_out_dim*self.num_filtrations
        else:
            in_out_dim = self.features_in + self.num_filtrations * self.total_num_coord_funs

        self.out = torch.nn.Linear(
            in_out_dim, features_out)

    def compute_persistence(self, x, batch):
        """
        Returns the persistence pairs as a list of tensors with shape [X.shape[0],2].
        The lenght of the list is the number of filtrations.
        """
        edge_index = batch.edge_index
        filtered_v_ = torch.cat([filtration_mod.forward(x)
                                 for filtration_mod in self.filtration_modules], 1)

        filtered_e_, _ = torch.max(torch.stack(
            (filtered_v_[edge_index[0]], filtered_v_[edge_index[1]])), axis=0)

        vertex_slices = torch.Tensor(batch.__slices__['x']).cpu().long()
        edge_slices = torch.Tensor(batch.__slices__['edge_index']).cpu().long()

        filtered_v_ = filtered_v_.cpu().transpose(1, 0).contiguous()
        filtered_e_ = filtered_e_.cpu().transpose(1, 0).contiguous()
        edge_index = edge_index.cpu().transpose(1, 0).contiguous()

        persistence0_new, persistence1_new = compute_persistence_homology_batched_mt(
            filtered_v_, filtered_e_, edge_index,
            vertex_slices, edge_slices)
        persistence0_new = persistence0_new.to(x.device)
        persistence1_new = persistence1_new.to(x.device)

        # TEST
        # persistence0 = parallel_persistence_routine(filtered_v_cpu, batch).to(filtered_v_.device)
        # persistence0 = torch.split(persistence0,1,2)
        # persistence0 = [p.squeeze(-1) for p in persistence0]

        # persistence0 = []
        # persistence1 = []
        # filtered_v_cpu = filtered_v_.cpu()

        # for f_idx in range(self.num_filtrations):
        #     batch_cpu = batch.clone().to("cpu")
        #     # TODO: Test on a single instance
        #     batch_p_ = batch_persistence_routine(
        #         filtered_v_cpu[:, f_idx], batch_cpu, self.dim1)

        #     if self.dim1:  # cycles were computed
        #         persistence0.append(batch_p_[0].to(filtered_v_.device))
        #         persistence1.append(batch_p_[1].to(filtered_v_.device))
        #     else:
        #         persistence0.append(batch_p_.to(filtered_v_.device))

        return persistence0_new, persistence1_new

    def compute_coord_fun(self, persistence, batch, dim1=False):
        """
        Input : persistence [N_points,2]
        Output : coord_fun mean-aggregated [self.num_coord_fun]
        """
        if dim1:
            if self.set2set:
                coord_activation = self.set_transformer1(
                    persistence, batch, dim1_flag=True)
            else:
                coord_activation = torch.cat(
                    [mod.forward(persistence) for mod in self.coord_fun_modules1], 1)
        else:

            if self.set2set:
                coord_activation = self.set_transformer(persistence, batch)
            else:
                coord_activation = torch.cat(
                    [mod.forward(persistence) for mod in self.coord_fun_modules], 1)

        return coord_activation

    def compute_coord_activations(self, persistences, batch, dim1=False):
        """
        Return the coordinate functions activations pooled by graph.
        Output dims : list of length number of filtrations with elements : [N_graphs in batch, number fo coordinate functions]
        """

        coord_activations = [self.compute_coord_fun(
            persistence, batch=batch, dim1=dim1) for persistence in persistences]
        return torch.cat(coord_activations, 1)

    def collapse_dim1(self, activations, mask, slices):
        """
        Takes a flattened tensor of activations along with a mask and collapses it (sum) to have a graph-wise features

        Inputs : 
        * activations [N_edges,d]
        * mask [N_edge]
        * slices [N_graphs]
        Output:
        * collapsed activations [N_graphs,d]
        """
        collapsed_activations = []
        for el in range(len(slices)-1):
            activations_el_ = activations[slices[el]:slices[el+1]]
            mask_el = mask[slices[el]:slices[el+1]]
            activations_el = activations_el_[mask_el].sum(axis=0)
            collapsed_activations.append(activations_el)

        return torch.stack(collapsed_activations)

    def forward(self, x, batch):

        persistences0, persistences1 = self.compute_persistence(x, batch)
        coord_activations = self.compute_coord_activations(
            persistences0, batch)

        concat_activations = torch.cat((x, coord_activations), 1)

        out_activations = self.out(concat_activations)

        if self.dim1:
            persistence1_mask = (persistences1 != 0).any(2).any(0)
            # TODO potential save here by only computing the activation on the masked persistences
            coord_activations1 = self.compute_coord_activations(
                persistences1, batch, dim1=True)

            graph_activations1 = self.collapse_dim1(coord_activations1, persistence1_mask, batch.__slices__[
                "edge_index"])  # returns a vector for each graph

        else:
            graph_activations1 = None

        return out_activations, graph_activations1


class FiltrationGCNModel(pl.LightningModule):
    """
    GCN Model with a Graph Filtration Readout function.
    """

    def __init__(self, hidden_dim, filtration_hidden, num_node_features, num_classes, task, num_filtrations, num_coord_funs, dim1=False, num_coord_funs1=None, lr=0.001, dropout_p=0.2, set2set=False, set_out_dim=32, **kwargs):
        """
        num_filtrations = number of filtration functions
        num_coord_funs = number of different coordinate function
        """
        super().__init__()
        self.save_hyperparameters()
        self.conv1 = GCNConv(num_node_features, hidden_dim)
        # self.conv2 = GCNConv(hidden_dim, hidden_dim)

        coord_funs = {"Triangle_transform": num_coord_funs,
                      "Gaussian_transform": num_coord_funs,
                      "Line_transform": num_coord_funs,
                      "RationalHat_transform": num_coord_funs
                      }

        coord_funs1 = {"Triangle_transform": num_coord_funs1,
                       "Gaussian_transform": num_coord_funs1,
                       "Line_transform": num_coord_funs1,
                       "RationalHat_transform": num_coord_funs1
                       }
        self.topo1 = TopologyLayer(
            hidden_dim, hidden_dim, num_filtrations=num_filtrations,
            num_coord_funs=coord_funs, filtration_hidden=filtration_hidden,
            dim1=dim1, num_coord_funs1=coord_funs1, set2set=set2set,
            set_out_dim=set_out_dim
        )
        self.conv3 = GCNConv(hidden_dim, hidden_dim)

        if task is Tasks.GRAPH_CLASSIFICATION:
            self.pooling_fun = global_mean_pool
        elif task is Tasks.NODE_CLASSIFICATION:
            if dim1:
                raise NotImplementedError(
                    "We don't yet support cycles for node classification.")

            def fake_pool(x, batch):
                return x
            self.pooling_fun = fake_pool
        else:
            raise RuntimeError('Unsupported task.')

        self.set2set = set2set
        self.dim1 = dim1
        # number of extra dimension for each embedding from cycles (dim1)
        if dim1:
            if self.set2set:
                cycles_dim = set_out_dim * num_filtrations
            else:
                cycles_dim = num_filtrations * \
                    np.array(list(coord_funs1.values())).sum()
        else:
            cycles_dim = 0

        self.classif = torch.nn.Sequential(torch.nn.Linear(hidden_dim+cycles_dim, hidden_dim),
                                           torch.nn.ReLU(),
                                           torch.nn.Linear(hidden_dim, num_classes))

        self.loss = torch.nn.CrossEntropyLoss()

        self.num_filtrations = num_filtrations
        self.num_coord_funs = num_coord_funs

        self.accuracy = pl.metrics.Accuracy()
        self.accuracy_val = pl.metrics.Accuracy()
        self.accuracy_test = pl.metrics.Accuracy()

        self.lr = lr
        self.dropout_p = dropout_p

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)

    def forward(self, data):
        x, edge_index = data.x, data.edge_index

        x = self.conv1(x, edge_index)
        x = F.relu(x)
        x = F.dropout(x, p=self.dropout_p, training=self.training)
        #x = self.conv2(x, edge_index)
        #x = F.relu(x)
        #x = F.dropout(x, training = self.training)

        x, x_dim1 = self.topo1(x, data)

        x = F.relu(x)
        x = F.dropout(x, p=self.dropout_p, training=self.training)

        x = self.conv3(x, edge_index)

        x = self.pooling_fun(x, data.batch)

        if self.dim1:
            x_pre_class = torch.cat([x, x_dim1], axis=1)
        else:
            x_pre_class = x

        x_out = self.classif(x_pre_class)

        return x_out

    def training_step(self, batch, batch_idx):
        y = batch.y

        y_hat = self(batch)

        loss = self.loss(y_hat, y)

        self.accuracy(y_hat, y)

        self.log("train_loss", loss, on_step=True, on_epoch=True)
        self.log("train_acc", self.accuracy, on_step=True, on_epoch=True)
        return loss

    # def training_epoch_end(self,outs):
    #    self.log("train_acc_epoch",self.accuracy.compute())

    def validation_step(self, batch, batch_idx):
        y = batch.y
        y_hat = self(batch)

        loss = self.loss(y_hat, y)
        self.log("val_loss", loss, on_epoch=True)

        self.accuracy_val(y_hat, y)
        self.log("val_acc", self.accuracy_val, on_epoch=True)

        return {"predictions": y_hat.detach().cpu(),
                "labels": y.detach().cpu()}

    def test_step(self, batch, batch_idx):
        y = batch.y
        y_hat = self(batch)

        loss = self.loss(y_hat, y)
        self.log("test_loss", loss, on_epoch=True)

        self.accuracy_test(y_hat, y)
        self.log("test_acc", self.accuracy_test, on_epoch=True)

    @classmethod
    def add_model_specific_args(cls, parent):
        parser = argparse.ArgumentParser(parents=[parent])
        parser.add_argument('--hidden_dim', type=int, default=34)
        parser.add_argument('--filtration_hidden', type=int, default=15)
        parser.add_argument('--num_filtrations', type=int, default=2)
        parser.add_argument('--dim1', type=str2bool, default=False)
        parser.add_argument('--num_coord_funs', type=int, default=3)
        parser.add_argument('--num_coord_funs1', type=int, default=3)
        parser.add_argument('--lr', type=float, default=0.005)
        parser.add_argument('--dropout_p', type=int, default=0.2)
        parser.add_argument('--set2set', type=str2bool, default=False)
        parser.add_argument('--set_out_dim', type=int, default=32)
        return parser


class GCNModel(pl.LightningModule):
    def __init__(self, hidden_dim, num_node_features, num_classes, task, lr=0.001, dropout_p=0.2, GIN=False, set2set=False, **kwargs):
        super().__init__()
        self.save_hyperparameters()

        if GIN:

            gin_net1 = torch.nn.Sequential(torch.nn.Linear(num_node_features, hidden_dim),
                                           torch.nn.ReLU(),
                                           torch.nn.Linear(hidden_dim, hidden_dim))

            gin_net2 = torch.nn.Sequential(torch.nn.Linear(hidden_dim, hidden_dim),
                                           torch.nn.ReLU(),
                                           torch.nn.Linear(hidden_dim, hidden_dim))

            self.conv1 = GINConv(nn=gin_net1)
            self.conv3 = GINConv(nn=gin_net2)

            if task is Tasks.GRAPH_CLASSIFICATION:
                self.pooling_fun = global_add_pool
            elif task is Tasks.NODE_CLASSIFICATION:
                def fake_pool(x, batch):
                    return x
                self.pooling_fun = fake_pool
            else:
                raise RuntimeError('Unsupported task.')

        else:
            self.conv1 = GCNConv(num_node_features, hidden_dim)
            # self.conv2 = GCNConv(hidden_dim, hidden_dim)
            self.conv3 = GCNConv(hidden_dim, hidden_dim)

            if task is Tasks.GRAPH_CLASSIFICATION:
                self.pooling_fun = global_mean_pool
            elif task is Tasks.NODE_CLASSIFICATION:
                def fake_pool(x, batch):
                    return x
                self.pooling_fun = fake_pool
            else:
                raise RuntimeError('Unsupported task.')

        if set2set:
            self.fake_topo = coord_transforms.Set2SetMod(
                dim_in=hidden_dim, dim_out=hidden_dim, num_heads=4, num_inds=256)

        else:
            self.fake_topo = torch.nn.Linear(hidden_dim, hidden_dim)

        self.classif = torch.nn.Sequential(torch.nn.Linear(hidden_dim, hidden_dim),
                                           torch.nn.ReLU(),
                                           torch.nn.Linear(hidden_dim, num_classes))

        self.loss = torch.nn.CrossEntropyLoss()

        self.accuracy = pl.metrics.Accuracy()
        self.accuracy_val = pl.metrics.Accuracy()
        self.accuracy_test = pl.metrics.Accuracy()

        self.lr = lr

        self.dropout_p = dropout_p

        self.set2set = set2set

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)

    def forward(self, data):
        x, edge_index = data.x, data.edge_index

        x = self.conv1(x, edge_index)
        x = F.relu(x)
        x = F.dropout(x, p=self.dropout_p, training=self.training)

        if self.set2set:
            x = self.fake_topo(x, data)
        else:
            x = self.fake_topo(x)

        x = F.relu(x)
        x = F.dropout(x, p=self.dropout_p, training=self.training)

        x = self.conv3(x, edge_index)

        x = self.pooling_fun(x, data.batch)

        x_out = self.classif(x)

        return x_out

    def training_step(self, batch, batch_idx):
        y = batch.y
        # Flatten to make graph classification the same as node classification
        y = y.view(-1)
        y_hat = self(batch)
        y_hat = y_hat.view(-1, y_hat.shape[-1])

        loss = self.loss(y_hat, y)

        self.accuracy(y_hat, y)

        self.log("train_loss", loss, on_step=True, on_epoch=True)
        self.log("train_acc", self.accuracy, on_step=True, on_epoch=True)
        return loss

    # def training_epoch_end(self,outs):
    #    self.log("train_acc_epoch",self.accuracy.compute())

    def validation_step(self, batch, batch_idx):
        y = batch.y
        # Flatten to make graph classification the same as node classification
        y = y.view(-1)
        y_hat = self(batch)
        y_hat = y_hat.view(-1, y_hat.shape[-1])

        loss = self.loss(y_hat, y)

        self.accuracy_val(y_hat, y)
        self.log("val_loss", loss)

        self.log("val_acc", self.accuracy_val, on_epoch=True)

        return {"predictions": y_hat.detach().cpu(),
                "labels": y.detach().cpu()}

    def test_step(self, batch, batch_idx):

        y = batch.y
        y_hat = self(batch)

        loss = self.loss(y_hat, y)
        self.log("test_loss", loss, on_epoch=True)

        self.accuracy_test(y_hat, y)

        self.log("test_acc", self.accuracy_test, on_epoch=True)

    @classmethod
    def add_model_specific_args(cls, parent):
        import argparse
        parser = argparse.ArgumentParser(parents=[parent])
        parser.add_argument("--hidden_dim", type=int, default=34)
        parser.add_argument("--lr", type=float, default=0.005)
        parser.add_argument("--dropout_p", type=float, default=0.1)
        parser.add_argument('--GIN', type=str2bool, default=False)
        parser.add_argument('--set2set', type=str2bool, default=False)
        return parser

    # def validation_epoch_end(self,outputs):
    #    self.log("val_acc_epoch", self.accuracy_val.compute())


class LargerGCNModel(pl.LightningModule):
    def __init__(self, hidden_dim, depth, num_node_features, num_classes, task,
                 lr=0.001, dropout_p=0.2, GIN=False, set2set=False,
                 batch_norm=False, residual=False, train_eps=True, **kwargs):
        super().__init__()
        self.save_hyperparameters()
        self.embedding = torch.nn.Linear(num_node_features, hidden_dim)

        if GIN:
            def build_gnn_layer():
                return GINLayer(hidden_dim, hidden_dim, train_eps=train_eps)
            graph_pooling_operation = global_add_pool
        else:
            def build_gnn_layer():
                return GCNLayer(
                    hidden_dim, hidden_dim, F.relu, dropout_p, batch_norm)
            graph_pooling_operation = global_mean_pool

        self.layers = nn.ModuleList([
            build_gnn_layer() for _ in range(depth)])

        if task is Tasks.GRAPH_CLASSIFICATION:
            self.pooling_fun = graph_pooling_operation
        elif task is Tasks.NODE_CLASSIFICATION:
            def fake_pool(x, batch):
                return x
            self.pooling_fun = fake_pool
        else:
            raise RuntimeError('Unsupported task.')

        self.classif = torch.nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(hidden_dim // 2, hidden_dim // 4),
            nn.ReLU(),
            nn.Linear(hidden_dim // 4, num_classes)
        )

        if task is Tasks.GRAPH_CLASSIFICATION:
            self.accuracy = pl.metrics.Accuracy()
            self.accuracy_val = pl.metrics.Accuracy()
            self.accuracy_test = pl.metrics.Accuracy()
            self.loss = torch.nn.CrossEntropyLoss()
        elif task is Tasks.NODE_CLASSIFICATION:
            self.accuracy = WeightedAccuracy()
            self.accuracy_val = WeightedAccuracy()
            self.accuracy_test = WeightedAccuracy()

            def weighted_loss(pred, label):
                # calculating label weights for weighted loss computation
                with torch.no_grad():
                    n_classes = pred.shape[1]
                    V = label.size(0)
                    label_count = torch.bincount(label)
                    label_count = label_count[label_count.nonzero(
                        as_tuple=True)].squeeze()
                    cluster_sizes = torch.zeros(
                        n_classes, dtype=torch.long, device=pred.device)
                    cluster_sizes[torch.unique(label)] = label_count
                    weight = (V - cluster_sizes).float() / V
                    weight *= (cluster_sizes > 0).float()
                return F.cross_entropy(pred, label, weight)

            self.loss = weighted_loss

        self.lr = lr

        self.lr_patience = kwargs["lr_patience"]

        self.min_lr = kwargs["min_lr"]



        self.dropout_p = dropout_p

    def configure_optimizers(self):
        """Reduce learning rate if val_loss doesnt improve."""
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
        scheduler =  {'scheduler':torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode='min', factor=0.5, patience=self.lr_patience,
            min_lr=self.min_lr),
            "monitor":"val_loss",
            "frequency":1,
            "interval":"epoch"}

        return [optimizer], [scheduler]

    def forward(self, data):
        x, edge_index = data.x, data.edge_index
        x = self.embedding(x)

        for layer in self.layers:
            x = layer(x, edge_index=edge_index, data=data)

        x = self.pooling_fun(x, data.batch)
        x = self.classif(x)

        return x

    def training_step(self, batch, batch_idx):
        y = batch.y
        # Flatten to make graph classification the same as node classification
        y = y.view(-1)
        y_hat = self(batch)
        y_hat = y_hat.view(-1, y_hat.shape[-1])

        loss = self.loss(y_hat, y)

        self.accuracy(y_hat, y)

        self.log("train_loss", loss, on_step=True, on_epoch=True)
        self.log("train_acc", self.accuracy, on_step=True, on_epoch=True)
        return loss

    def validation_step(self, batch, batch_idx):
        y = batch.y
        # Flatten to make graph classification the same as node classification
        y = y.view(-1)
        y_hat = self(batch)
        y_hat = y_hat.view(-1, y_hat.shape[-1])

        loss = self.loss(y_hat, y)

        self.accuracy_val(y_hat, y)

        self.log("val_loss", loss, on_epoch = True)

        self.log("val_acc", self.accuracy_val, on_epoch=True)

    def test_step(self, batch, batch_idx):

        y = batch.y
        y_hat = self(batch)

        loss = self.loss(y_hat, y)
        self.log("test_loss", loss, on_epoch=True)

        self.accuracy_test(y_hat, y)

        self.log("test_acc", self.accuracy_test, on_epoch=True)

    @ classmethod
    def add_model_specific_args(cls, parent):
        import argparse
        parser = argparse.ArgumentParser(parents=[parent])
        parser.add_argument("--hidden_dim", type=int, default=146)
        parser.add_argument("--depth", type=int, default=4)
        parser.add_argument("--lr", type=float, default=0.001)
        parser.add_argument("--lr_patience", type=int, default=10)
        parser.add_argument("--min_lr", type=float, default=0.00001)
        parser.add_argument("--dropout_p", type=float, default=0.0)
        parser.add_argument('--GIN', type=str2bool, default=False)
        parser.add_argument('--set2set', type=str2bool, default=False)
        parser.add_argument('--batch_norm', type=str2bool, default=True)
        parser.add_argument('--residual', type=str2bool, default=True)
        return parser
