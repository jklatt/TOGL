"""Analyse set of graphs using Weisfeiler--Lehman feature iteration."""

import argparse
import pickle
import torch
import sys

import igraph as ig
import numpy as np

from sklearn.linear_model import LogisticRegressionCV
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics.pairwise import euclidean_distances

from weisfeiler_lehman import WeisfeilerLehman


def build_graph_from_edge_list(edge_list):
    """Build graph from edge list and return it."""
    n_vertices = edge_list.max().numpy() + 1
    g = ig.Graph(n_vertices)

    for u, v in edge_list.numpy().transpose():
        g.add_edge(u, v)

    g.vs['label'] = g.degree()
    return g


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('INPUT', type=str, nargs='+', help='Input file(s)')
    parser.add_argument(
        '-H', '--num-iterations',
        default=3,
        type=int,
        help='Number of iterations for the Weisfeiler--Lehman algorithm'
    )
    parser.add_argument(
        '-p', '--pickle',
        action='store_true',
        help='If set, loads graphs from pickle file'
    )
    parser.add_argument(
        '-l', '--labels',
        type=str,
        help='Path to labels'
    )

    args = parser.parse_args()
    H = args.num_iterations

    # Will contain all graphs in `igraph` format. They will form the
    # basis for the analysis in terms of Weisfeiler--Lehman features
    # later on.
    graphs = []

    # Ditto for labels, but this is optional.
    labels = []

    if args.labels is not None:
        labels = torch.load(args.labels).numpy()

    for filename in args.INPUT:
        if args.pickle:
            with open(filename, 'rb') as f:
                x_list, edge_lists = pickle.load(f)

                for edge_list in edge_lists:
                    graphs.append(build_graph_from_edge_list(edge_list))
        else:
            g = ig.Graph.Read_Edgelist(filename, directed=False)
            g.vs['label'] = g.degree()

            graphs.append(g)

    # First analysis step: degree distribution
    #
    # The idea is to obtain a mean degree distribution that does not
    # depend on the number of cycles.
    X = []

    for g in graphs:
        degrees = g.degree()
        X.append(np.bincount(degrees))

    X = np.asarray(X)

    print('Mean degree', np.mean(X, axis=0))

    # Second analysis step: Weisfeiler--Lehman feature vectors
    #
    # The idea is to show that the feature vectors are the same between
    # two distributions of graphs (or require more steps than warranted
    # as the cycle length increases).

    wl = WeisfeilerLehman()
    label_dicts = wl.fit_transform(graphs, num_iterations=H)

    # Will contain the feature matrix. Rows are indexing individual
    # graphs, columns are indexing all iterations of the scheme, so
    # that the full WL iteration is contained in one vector.
    X = []

    for i, g in enumerate(graphs):

        # All feature vectors of the current graph
        x = []
        for h in range(H):
            _, compressed_labels = label_dicts[h][i]
            x.extend(np.bincount(compressed_labels).tolist())

        X.append(x)

    # Ensure that all feature vectors have the same length.

    L = 0
    for x in X:
        L = max(L, len(x))

    X = [x + [0] * (L - len(x)) for x in X]
    X = np.asarray(X)

    # Norm distribution of all vectors; not sure whether this will be
    # useful.
    norms = np.sqrt(np.sum(np.abs(X)**2, axis=-1))
    print(f'Norm distribution of WL feature vectors: {norms}')

    distances = euclidean_distances(X)
    print(f'Mean distance between WL feature vectors: {np.mean(distances)}')

    if args.labels is None:
        sys.exit(0)

    print('Fitting logistic regression (cross-validated) on data...')

    scores = []

    for i in range(10):
        cv = StratifiedKFold(n_splits=5, shuffle=True)
        clf = LogisticRegressionCV(Cs=10, cv=cv)
        clf.fit(X, labels)

        score = clf.score(X, labels)
        scores.append(score)

        print(f'Iteration {i}: {100 * score:.2f}')

    print(f'{100 * np.mean(scores):.2f} +- {100 * np.std(scores):.2f}')

    y_pred = clf.predict(X)

    n = 0

    for i, g in enumerate(graphs):
        indices = np.nonzero(distances[i, :] == 0)

        #if len(indices) != 0 and (labels[indices] != labels[i]).any():
        #    layout = graphs[i].layout('kk')
        #    ig.plot(graphs[i], layout=layout)

        for index in indices[0]:
            if labels[index] != labels[i]:
                pass
                #print(index)

                #layout = graphs[index].layout('kk')
                #ig.plot(graphs[index], layout=layout)

        #print('NEXT GRAPHS')

        distances_other = distances[i, labels != labels[i]]
        n += np.sum(distances_other == 0)

    print(n // 2)

    #for g in np.asarray(graphs)[y_pred != labels]:
    #    layout = g.layout('kk')
    #    ig.plot(g, layout=layout)
