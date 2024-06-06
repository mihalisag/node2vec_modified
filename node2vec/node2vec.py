import os
import random
from collections import defaultdict

import gensim
import networkx as nx
import numpy as np
import pkg_resources
from joblib import Parallel, delayed
from tqdm.auto import tqdm

from .parallel import parallel_generate_walks
# from .parallel import parallel_generate_walks

# --------
# My code

import pickle
from datetime import datetime


def generate_timestamp():
    '''
        Generates timestamp in the format YYMMDD_HHMMSS
    '''
    # Get the current date and time
    current_datetime = datetime.now()

    # Format the date and time as required
    formatted_datetime = current_datetime.strftime("%Y%m%d_%H%M%S")

    # Return the formatted date and time
    return formatted_datetime

def save_walks(graph, walks, r, l, p, q, ns):#, dataset):
    '''
        Save walks in a .pkl format
    '''

    folder_path = 'walks'
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)

    filename = f"{graph.name}_walks_r{r}_l{l}_p{p}_q{q}_ns_{ns}.pkl"
    file_path = os.path.join(folder_path, filename)

    # Open the file in binary write mode
    with open(file_path, "wb") as file:
        # Serialize the list of lists using pickle.dump
        pickle.dump(walks, file)

    return file_path
        
# -------    
    
    
class Node2Vec:
    FIRST_TRAVEL_KEY = 'first_travel_key'
    PROBABILITIES_KEY = 'probabilities'
    NEIGHBORS_KEY = 'neighbors'
    WEIGHT_KEY = 'weight'
    NUM_WALKS_KEY = 'num_walks'
    WALK_LENGTH_KEY = 'walk_length'
    P_KEY = 'p'
    Q_KEY = 'q'

    def __init__(self, graph: nx.Graph, dimensions: int = 128, walk_length: int = 80, num_walks: int = 10, p: float = 1,
                 q: float = 1, weight_key: str = 'weight', workers: int = 1, sampling_strategy: dict = None,
                 quiet: bool = False, temp_folder: str = None, seed: int = None, starting_nodes: list = None):
        """
        Initiates the Node2Vec object, precomputes walking probabilities and generates the walks.

        :param graph: Input graph
        :param dimensions: Embedding dimensions (default: 128)
        :param walk_length: Number of nodes in each walk (default: 80)
        :param num_walks: Number of walks per node (default: 10)
        :param p: Return hyper parameter (default: 1)
        :param q: Inout parameter (default: 1)
        :param weight_key: On weighted graphs, this is the key for the weight attribute (default: 'weight')
        :param workers: Number of workers for parallel execution (default: 1)
        :param sampling_strategy: Node specific sampling strategies, supports setting node specific 'q', 'p', 'num_walks' and 'walk_length'.
        :param seed: Seed for the random number generator.
        Use these keys exactly. If not set, will use the global ones which were passed on the object initialization
        :param temp_folder: Path to folder with enough space to hold the memory map of self.d_graph (for big graphs); to be passed joblib.Parallel.temp_folder
        """

        self.graph = graph
        self.dimensions = dimensions
        self.walk_length = walk_length
        self.num_walks = num_walks
        self.p = p
        self.q = q
        self.weight_key = weight_key
        self.workers = workers
        self.quiet = quiet
        self.d_graph = defaultdict(dict)
        self.starting_nodes = starting_nodes # ADDITION

        if sampling_strategy is None:
            self.sampling_strategy = {}
        else:
            self.sampling_strategy = sampling_strategy

        self.temp_folder, self.require = None, None
        if temp_folder:
            if not os.path.isdir(temp_folder):
                raise NotADirectoryError("temp_folder does not exist or is not a directory. ({})".format(temp_folder))

            self.temp_folder = temp_folder
            self.require = "sharedmem"

        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        self._precompute_probabilities()
        self.walks = self._generate_walks()

        #print(d_graph)


    def _precompute_probabilities(self):
        """
        Precomputes transition probabilities for each node.
        """

        print('Computing transition probabilities...')

        d_graph = self.d_graph

        nodes_generator = self.graph.nodes() if self.quiet \
            else tqdm(self.graph.nodes(), desc='Computing transition probabilities')

        for source in nodes_generator:

            # Init probabilities dict for first travel
            if self.PROBABILITIES_KEY not in d_graph[source]:
                d_graph[source][self.PROBABILITIES_KEY] = dict()

            for current_node in self.graph.neighbors(source):

                # Init probabilities dict
                if self.PROBABILITIES_KEY not in d_graph[current_node]:
                    d_graph[current_node][self.PROBABILITIES_KEY] = dict()

                unnormalized_weights = list()
                d_neighbors = list()

                # Calculate unnormalized weights
                for destination in self.graph.neighbors(current_node):

                    p = self.sampling_strategy[current_node].get(self.P_KEY,
                                                                 self.p) if current_node in self.sampling_strategy else self.p
                    q = self.sampling_strategy[current_node].get(self.Q_KEY,
                                                                 self.q) if current_node in self.sampling_strategy else self.q

                    try:
                        if self.graph[current_node][destination].get(self.weight_key):
                            weight = self.graph[current_node][destination].get(self.weight_key, 1)
                        else: 
                            ## Example : AtlasView({0: {'type': 1, 'weight':0.1}})- when we have edge weight
                            edge = list(self.graph[current_node][destination])[-1]
                            weight = self.graph[current_node][destination][edge].get(self.weight_key, 1)
                            
                    except:
                        weight = 1 
                    
                    if destination == source:  # Backwards probability
                        ss_weight = weight * 1 / p
                    elif destination in self.graph[source]:  # If the neighbor is connected to the source
                        ss_weight = weight
                    else:
                        ss_weight = weight * 1 / q

                    # Assign the unnormalized sampling strategy weight, normalize during random walk
                    unnormalized_weights.append(ss_weight)
                    d_neighbors.append(destination)

                # Normalize
                unnormalized_weights = np.array(unnormalized_weights)
                d_graph[current_node][self.PROBABILITIES_KEY][
                    source] = unnormalized_weights / unnormalized_weights.sum()

            # Calculate first_travel weights for source
            first_travel_weights = []

            for destination in self.graph.neighbors(source):
                first_travel_weights.append(self.graph[source][destination].get(self.weight_key, 1))

            first_travel_weights = np.array(first_travel_weights)
            d_graph[source][self.FIRST_TRAVEL_KEY] = first_travel_weights / first_travel_weights.sum()

            # Save neighbors
            d_graph[source][self.NEIGHBORS_KEY] = list(self.graph.neighbors(source))


        # Maybe add here a line where the d_graph gets updated with 
        # a new d_graph for the updated small graph?

    def _generate_walks(self) -> list:
        """
        Generates the random walks which will be used as the skip-gram input.
        :param starting_nodes: List of nodes to start the random walks from.
        :return: List of walks. Each walk is a list of nodes.
        """

        flatten = lambda l: [item for sublist in l for item in sublist]

        # Split num_walks for each worker
        num_walks_lists = np.array_split(range(self.num_walks), self.workers)

        print('Random walks in progress...')
        walk_results = tqdm(Parallel(n_jobs=self.workers, temp_folder=self.temp_folder, require=self.require)(
            delayed(parallel_generate_walks)(self.d_graph,
                                            self.walk_length,
                                            len(num_walks),
                                            idx,
                                            self.sampling_strategy,
                                            self.NUM_WALKS_KEY,
                                            self.WALK_LENGTH_KEY,
                                            self.NEIGHBORS_KEY,
                                            self.PROBABILITIES_KEY,
                                            self.FIRST_TRAVEL_KEY,
                                            self.quiet,
                                            self.starting_nodes) for
            idx, num_walks
            in enumerate(num_walks_lists, 1)))

        walks = flatten(walk_results)
        
        # ---
        
        filename = save_walks(self.graph, walks, self.num_walks, self.walk_length, self.p, self.q, ns=0.75)#, dataset)
        
        if not self.quiet:
            print(f"Saved random walks as {filename}")

        # ---
    
        return walks

    
    def fit(self, **skip_gram_params) -> gensim.models.Word2Vec:
        """
        Creates the embeddings using gensim's Word2Vec.
        :param skip_gram_params: Parameters for gensim.models.Word2Vec - do not supply 'size' / 'vector_size' it is
            taken from the Node2Vec 'dimensions' parameter
        :type skip_gram_params: dict
        :return: A gensim word2vec model
        """

        print('Fitting model...')

        if 'workers' not in skip_gram_params:
            skip_gram_params['workers'] = self.workers

        # Figure out gensim version, naming of output dimensions changed from size to vector_size in v4.0.0
        gensim_version = pkg_resources.get_distribution("gensim").version
        size = 'size' if gensim_version < '4.0.0' else 'vector_size'
        if size not in skip_gram_params:
            skip_gram_params[size] = self.dimensions

        if 'sg' not in skip_gram_params:
            skip_gram_params['sg'] = 1

        # Maybe here update self.walks parameter before passing it to Word2vec 
        # to include the updated graph ones. Here it is more suitable from the
        # generate_walks function as it would get more complicated and here we
        # just pass the walks as a parameter (?)

        return gensim.models.Word2Vec(self.walks, **skip_gram_params)
