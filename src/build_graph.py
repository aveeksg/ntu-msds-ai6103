import argparse
import os
# from pickletools import optimize
import random
import string
import time
import json
from math import log
from pathlib import Path
from datetime import datetime
import pytz

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # del
os.environ['CUDA_VISIBLE_DEVICES'] = "0"

import numpy as np
import scipy.sparse as sp
from nltk.corpus import stopwords
from stanfordcorenlp import StanfordCoreNLP
from torch import Tensor, nn
from tqdm import tqdm
import torch
from torch.optim import AdamW
from torch.utils.data import TensorDataset, RandomSampler, SequentialSampler, DataLoader
from utils import pickle_graph, set_torch_seed, setup_logging




class LSTM_classifier(nn.Module):
    def __init__(self, vocab_size, emb_size, hidden_size, num_labels, dropout, num_layers=1) -> None:
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, emb_size)
        self.lstm = nn.LSTM(input_size=emb_size, hidden_size=hidden_size,num_layers=num_layers, batch_first=True, dropout=dropout, bidirectional=False)
        self.classifier = nn.Linear(hidden_size, num_labels)
    def forward(self, inputs):
        emb = self.embedding(inputs)
        output, (h_n, c_n) = self.lstm(emb)
        inter_output = torch.mean(output, dim=1)
        res = self.classifier(inter_output)
        return output, res



def gen_syn(corpus, nlp:StanfordCoreNLP, row_tfidf, col_tfidf, weight_tfidf, word_id_map, node_size, train_size):
    '''
    calculate syntactic relationship over words in the corpus
    input:
        corpus: a list that contains sentences/documents (strings)
        pmi: a dict that maps word pair to pmi
    '''
    t = time.time()
    stop_words = set(stopwords.words('english'))

    #获取句法依存关系对
    rela_pair_count_str = {} 
    for doc_id in tqdm(range(len(corpus))):
        # print(doc_id)
        words = corpus[doc_id]
        words = words.split("\n")
        rela=[]
        for window in words:
            if not window.strip():
                continue
            #构造rela_pair_count
            window = window.replace(string.punctuation, ' ')
            try:
                r_dict = nlp._request('depparse', window)
            except json.decoder.JSONDecodeError:
                continue
            res = [(dep['governorGloss'], dep['dependentGloss']) for s in r_dict['sentences'] for dep in
            s['basicDependencies']]
            for tuple in res:
                rela.append(tuple[0] + ', ' + str(tuple[1]))
            for pair in rela:
                pair=pair.split(", ")
                if pair[0]=='ROOT' or pair[1]=='ROOT':
                    continue
                if pair[0] == pair[1]:
                    continue
                if pair[0] in string.punctuation or pair[1] in string.punctuation:
                    continue
                if pair[0] in stop_words or pair[1] in stop_words:
                    continue
                word_pair_str = pair[0] + ',' + pair[1]
                if word_pair_str in rela_pair_count_str:
                    rela_pair_count_str[word_pair_str] += 1
                else:
                    rela_pair_count_str[word_pair_str] = 1
                # two orders
                word_pair_str = pair[1] + ',' + pair[0]
                if word_pair_str in rela_pair_count_str:
                    rela_pair_count_str[word_pair_str] += 1
                else:
                    rela_pair_count_str[word_pair_str] = 1
    max_count = 0
    min_count = 1000000
    for v in rela_pair_count_str.values():
        if v < min_count:
            min_count = v
        if v > max_count:
            max_count = v
    graph = []
    row, col = [],[]
    for key in rela_pair_count_str:
        temp = key.split(',')
        if temp[0] not in word_id_map or temp[1] not in word_id_map:
            continue
        i = word_id_map[temp[0]]
        j = word_id_map[temp[1]]
        row.append(train_size + i)
        col.append(train_size + j)  
        w = (rela_pair_count_str[key] - min_count) / (max_count - min_count)
        graph.append(w)
    weight = graph + weight_tfidf
    num_edges = len(row)
    row = row + row_tfidf
    col = col + col_tfidf
    adj = sp.csr_matrix(
        (weight, (row, col)), shape=(node_size, node_size))
    logger.info("Syntactic graph finish! Time spent {:2f} number of edges {}".format(time.time()-t, num_edges))
    return adj

def gen_syn_bidirect(corpus, nlp: StanfordCoreNLP, row_tfidf, col_tfidf, weight_tfidf, word_id_map, node_size, train_size):
    '''
    Calculate syntactic relationship over words in the corpus with bidirectional edge weights.
    Input:
        corpus: a list that contains sentences/documents (strings)
        pmi: a dict that maps word pair to pmi
    '''
   
    t = time.time()
    stop_words = set(stopwords.words('english'))

    rela_pair_count_str = {} 
    for doc_id in tqdm(range(len(corpus))):
        words = corpus[doc_id]
        words = words.split("\n")
        rela=[]
        for window in words:
            if not window.strip():
                continue
            window = window.replace(string.punctuation, ' ')
            try:
                r_dict = nlp._request('depparse', window)
            except json.decoder.JSONDecodeError:
                continue
            res = [(dep['governorGloss'], dep['dependentGloss']) for s in r_dict['sentences'] for dep in
                   s['basicDependencies']]
            for tuple in res:
                rela.append(tuple[0] + ', ' + str(tuple[1]))
            for pair in rela:
                pair = pair.split(", ")
                if 'ROOT' in pair:
                    continue
                if pair[0] == pair[1] or pair[0] in string.punctuation or pair[1] in string.punctuation:
                    continue
                if pair[0] in stop_words or pair[1] in stop_words:
                    continue
                # Increment weight for both orders simultaneously
                for perm in [(pair[0], pair[1]), (pair[1], pair[0])]:
                    word_pair_str = perm[0] + ',' + perm[1]
                    if word_pair_str in rela_pair_count_str:
                        rela_pair_count_str[word_pair_str] += 1
                    else:
                        rela_pair_count_str[word_pair_str] = 1

    max_count = max(rela_pair_count_str.values(), default=0)
    min_count = min(rela_pair_count_str.values(), default=0)
    
    graph = []
    row, col = [], []
    for key, count in rela_pair_count_str.items():
        temp = key.split(',')
        if temp[0] not in word_id_map or temp[1] not in word_id_map:
            continue
        i = word_id_map[temp[0]]
        j = word_id_map[temp[1]]
        normalized_weight = (count - min_count) / (max_count - min_count)
        row.extend([train_size + i, train_size + j])
        col.extend([train_size + j, train_size + i])  # Ensure symmetric indices
        graph.extend([normalized_weight, normalized_weight])  # Ensure symmetric weights
    
    weight = graph + weight_tfidf
    num_edges = len(row)
    row = row + row_tfidf
    col = col + col_tfidf
    adj = sp.csr_matrix((weight, (row, col)), shape=(node_size, node_size))    
    print("Syntactic graph finished! Time spent {:.2f} seconds, number of edges {}".format(time.time()-t, num_edges))
    
    return adj
    
def gen_syn_tfidf_scaling(corpus, nlp:StanfordCoreNLP, row_tfidf, col_tfidf, weight_tfidf, word_id_map, node_size, train_size):
    '''
    calculate syntactic relationship over words in the corpus
    input:
        corpus: a list that contains sentences/documents (strings)
        pmi: a dict that maps word pair to pmi
    '''
    t = time.time()
    stop_words = set(stopwords.words('english'))

    #获取句法依存关系对
    rela_pair_count_str = {} 
    tfidf_map = {(row_tfidf[i], col_tfidf[i]): weight_tfidf[i] for i in range(len(weight_tfidf))}
    for doc_id in tqdm(range(len(corpus))):
        # print(doc_id)
        words = corpus[doc_id]
        words = words.split("\n")
        rela=[]
        for window in words:
            if not window.strip():
                continue
            #构造rela_pair_count
            window = window.replace(string.punctuation, ' ')
            try:
                r_dict = nlp._request('depparse', window)
            except json.decoder.JSONDecodeError:
                continue
            res = [(dep['governorGloss'], dep['dependentGloss']) for s in r_dict['sentences'] for dep in
            s['basicDependencies']]
            for tuple in res:
                rela.append(tuple[0] + ', ' + str(tuple[1]))
            for pair in rela:
                pair=pair.split(", ")
                if pair[0]=='ROOT' or pair[1]=='ROOT':
                    continue
                if pair[0] == pair[1]:
                    continue
                if pair[0] in string.punctuation or pair[1] in string.punctuation:
                    continue
                if pair[0] in stop_words or pair[1] in stop_words:
                    continue
                word_pair_str = pair[0] + ',' + pair[1]
                if word_pair_str in rela_pair_count_str:
                    rela_pair_count_str[word_pair_str] += 1
                else:
                    rela_pair_count_str[word_pair_str] = 1
                # two orders
                word_pair_str = pair[1] + ',' + pair[0]
                if word_pair_str in rela_pair_count_str:
                    rela_pair_count_str[word_pair_str] += 1
                else:
                    rela_pair_count_str[word_pair_str] = 1
    max_count = max(rela_pair_count_str.values(), default=1)
    min_count = min(rela_pair_count_str.values(), default=0)
    
    graph = []
    row, col = [], []
    for key, count in rela_pair_count_str.items():
        temp = key.split(',')
        if temp[0] not in word_id_map or temp[1] not in word_id_map:
            continue
        i = word_id_map[temp[0]]
        j = word_id_map[temp[1]]
        row.extend([train_size + i, train_size + j])
        col.extend([train_size + j, train_size + i])
        base_weight = (count - min_count) / (max_count - min_count)

        # Apply TF-IDF scaling
        tfidf_weight1 = tfidf_map.get((train_size + i, train_size + j), 0)
        tfidf_weight2 = tfidf_map.get((train_size + j, train_size + i), 0)
        weight = base_weight * (tfidf_weight1 + tfidf_weight2) / 2
        graph.extend([weight, weight])
    num_edges = len(row)
    adj = sp.csr_matrix((graph, (row, col)), shape=(node_size, node_size))
    print("Syntactic graph finish! Time spent {:2f} number of edges {}".format(time.time()-t, num_edges))
    return adj

def trans_corpus_to_ids(corpus, word_id_map, max_len):
    new_corpus = []
    for text in corpus:
        word_list = text.split()
        if len(word_list) > max_len:
            word_list = word_list[:max_len]
        new_corpus.append([word_id_map[w] + 1 for w in word_list]) # + 1 for padding
    # padding
    for i, one in enumerate(new_corpus):
        if len(one) < max_len:
            new_corpus[i] = one + [0]*(max_len-len(one))
    new_corpus = np.asarray(new_corpus, dtype=np.int32)
    return new_corpus

def lstm_eval(model, dataloader, device):
    model.eval()
    all_preds, all_labels,all_outs = [],[],[]
    for batch in dataloader:
        batch = [one.to(device) for one in batch]
        x, y = batch
        with torch.no_grad():
            output, pred = model(x)
            all_outs.append(output.cpu().numpy())
            pred_ids = torch.argmax(pred, dim=-1)
            all_preds += pred_ids.tolist()
            all_labels += y.tolist()
    acc = np.mean(np.asarray(all_preds) == np.asarray(all_labels))
    all_outs = np.concatenate(all_outs, axis=0)

    model.train()
    return acc, all_outs

def train_lstm(corpus, word_id_map, train_size, valid_size, labels, emb_size, hidden_size, dropout, batch_size, epochs, lr, weight_decay, num_labels,device,max_len, dataset, graphs_saved_path, num_layers):
    vocab_size = len(word_id_map) + 1
    corpus_ids = trans_corpus_to_ids(corpus, word_id_map, max_len)
    model = LSTM_classifier(vocab_size, emb_size, hidden_size, num_labels, dropout, num_layers=num_layers)
    model.to(device)
    train_data = corpus_ids[:train_size,:]
    dev_data = corpus_ids[train_size:train_size+valid_size,:]
    test_data = corpus_ids[train_size+valid_size:,:]
    train_label = labels[:train_size]
    dev_label = labels[train_size:train_size+valid_size]
    test_label = labels[train_size+valid_size:]
    train_x = torch.tensor(train_data, dtype=torch.long)
    train_y = torch.tensor(train_label, dtype=torch.long)
    dev_x = torch.tensor(dev_data, dtype=torch.long)
    dev_y = torch.tensor(dev_label, dtype=torch.long)
    test_x = torch.tensor(test_data, dtype=torch.long)
    test_y = torch.tensor(test_label, dtype=torch.long)
    train_dataset = TensorDataset(train_x, train_y)
    dev_dataset = TensorDataset(dev_x, dev_y)
    test_dataset = TensorDataset(test_x, test_y)
    train_sampler = RandomSampler(train_dataset)
    train_dev_sampler = SequentialSampler(train_dataset)
    dev_sampler = SequentialSampler(dev_dataset)
    test_sampler = SequentialSampler(test_dataset)
    train_dataloader = DataLoader(train_dataset,batch_size,sampler=train_sampler)
    train_dev_dataloader = DataLoader(train_dataset,batch_size,sampler=train_dev_sampler)
    dev_dataloader = DataLoader(dev_dataset,batch_size,sampler=dev_sampler)
    test_dataloader = DataLoader(test_dataset,batch_size,sampler=test_sampler)
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    model.train()
    loss_func = nn.CrossEntropyLoss(reduction='mean')
    best_acc = 0.0

    # training LSTM
    if epochs > 0:
        for ep in range(epochs):
            logger.info("Starting epochs [{}/{}]".format(ep+1, epochs))
            for batch in tqdm(train_dataloader):
                batch = [one.to(device) for one in batch]
                x, y = batch
                output, pred = model(x)
                loss = loss_func(pred, y)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            acc, all_outs = lstm_eval(model, dev_dataloader, device)
            if acc > best_acc:
                best_acc = acc
                logger.info("Saving semantic model into {}_lstm.bin".format(dataset))

                # create directory for saving LSTM 
                # graphs_saved_path = "saved_graphs/run_{}".format(timestamp)
                # if not os.path.exists(graphs_saved_path):
                #     os.makedirs(graphs_saved_path)
                torch.save(model.state_dict(), os.path.join(graphs_saved_path, '{}_lstm.bin'.format(dataset)))
                logger.info("current best acc={:4f}".format(acc))
    else:
        # CRITICAL - this part is illogical
        logger.info("loading lstm model")
        model.load_state_dict(torch.load('lstm.bin'))
    acc, all_outs_train = lstm_eval(model, train_dev_dataloader, device)
    acc, all_outs_dev = lstm_eval(model, dev_dataloader, device)
    acc, all_outs_test = lstm_eval(model, test_dataloader, device)
    all_outs = np.concatenate([all_outs_train, all_outs_dev, all_outs_test], axis=0)
    return model, all_outs, corpus_ids  

def gen_sem(args, corpus, word_id_map, row_tfidf, col_tfidf, weight_tfidf, thres, train_size, valid_size, labels, num_labels, node_size, device, graphs_saved_path):

    t = time.time()

    # training LSTM
    model, all_outs, corpus_ids = train_lstm(corpus, word_id_map, train_size, valid_size, labels, args.embed_size, args.hidden_size, args.dropout, args.batch_size, args.epochs, args.lr, args.weight_decay, num_labels,device, args.max_len, args.dataset, graphs_saved_path, args.lstm_layers)

    logger.info("Training LSTM completed")

    logger.info("all_outs:\n {}".format(all_outs))

    num_docs = all_outs.shape[0]
    test_ids = corpus_ids[train_size+valid_size:,:]
    cos_simi_count = {}

    for i in tqdm(range(num_docs)):
        text = corpus[i]
        word_list = text.split()
        max_len = len(word_list) if len(word_list) < args.max_len else args.max_len
        x = all_outs[i,:,:]
        x_norm = np.linalg.norm(x, ord=2, axis=-1, keepdims=True)
        simi_mat = np.dot(x, x.T) / np.dot(x_norm, x_norm.T) # L * L
        for k in range(max_len):
            for j in range(k+1, max_len):
                word_k_id = word_id_map[word_list[k]]
                word_j_id = word_id_map[word_list[j]]
                simi = simi_mat[k,j]
                if word_k_id == word_j_id:
                    continue
                if simi > thres:
                    word_pair_str = str(word_k_id) + ',' + str(word_j_id)
                    if word_pair_str in cos_simi_count:
                        cos_simi_count[word_pair_str] += 1
                    else:
                        cos_simi_count[word_pair_str] = 1
                    # two orders
                    word_pair_str = str(word_j_id) + ',' + str(word_k_id)
                    if word_pair_str in cos_simi_count:
                        cos_simi_count[word_pair_str] += 1
                    else:
                        cos_simi_count[word_pair_str] = 1
    
    max_count = 0
    min_count = 1000000
    row, col = [],[]
    for v in cos_simi_count.values():
        if v < min_count:
            min_count = v
        if v > max_count:
            max_count = v
    
    graph = []
    for key in cos_simi_count:
        temp = key.split(',')
        # if temp[0] not in word_id_map or temp[1] not in word_id_map:
        #     continue
        i = int(temp[0])
        j = int(temp[1])
        w = (cos_simi_count[key] - min_count) / (max_count - min_count)
        row.append(train_size + i)
        col.append(train_size + j)    
        graph.append(w)

    weight = graph + weight_tfidf
    num_edges = len(row)
    row = row + row_tfidf
    col = col + col_tfidf
    adj = sp.csr_matrix(
        (weight, (row, col)), shape=(node_size, node_size))
    logger.info("Semantic graph finish! Time spent {:2f} number of edges {}".format(time.time()-t, num_edges))

    return adj

def gen_seq(corpus, train_size, test_size, window_size, word_id_map, row_tfidf, col_tfidf, weight_tfidf, vocab):
    windows = []
    row, col, weight = [],[],[]
    t = time.time()
    vocab_size = len(vocab)
    logger.info("Generating sequential graph...")
    logger.info("windows generating...")
    for doc_words in corpus:
        words = doc_words.split()
        length = len(words)
        if length <= window_size:
            windows.append(words)
        else:
            # print(length, length - window_size + 1)
            for j in range(length - window_size + 1):
                window = words[j: j + window_size]
                windows.append(window)
                # print(window)
    logger.info("calculating word frequency...")
    word_window_freq = {}
    for window in tqdm(windows):
        appeared = set()
        for i in range(len(window)):
            if window[i] in appeared:
                continue
            if window[i] in word_window_freq:
                word_window_freq[window[i]] += 1
            else:
                word_window_freq[window[i]] = 1
            appeared.add(window[i])
    logger.info("calculating word pair frequency...")
    word_pair_count = {}
    for window in windows:
        for i in range(1, len(window)):
            for j in range(0, i):
                word_i = window[i]
                word_i_id = word_id_map[word_i]
                word_j = window[j]
                word_j_id = word_id_map[word_j]
                if word_i_id == word_j_id:
                    continue
                word_pair_str = str(word_i_id) + ',' + str(word_j_id)
                if word_pair_str in word_pair_count:
                    word_pair_count[word_pair_str] += 1
                else:
                    word_pair_count[word_pair_str] = 1
                # two orders
                word_pair_str = str(word_j_id) + ',' + str(word_i_id)
                if word_pair_str in word_pair_count:
                    word_pair_count[word_pair_str] += 1
                else:
                    word_pair_count[word_pair_str] = 1
    num_window = len(windows)
    pmi_dict = {}
    logger.info("calculating pmi...")
    for key in word_pair_count:
        temp = key.split(',')
        i = int(temp[0])
        j = int(temp[1])
        count = word_pair_count[key]
        word_freq_i = word_window_freq[vocab[i]]
        word_freq_j = word_window_freq[vocab[j]]
        pmi = log((1.0 * count / num_window) /
                (1.0 * word_freq_i * word_freq_j / (num_window * num_window)))
        if pmi <= 0:
            continue
        row.append(train_size + i)
        col.append(train_size + j)
        weight.append(pmi)
        pmi_dict[key] = pmi
    logger.info("create pmi graph...")
    weight = weight + weight_tfidf
    num_edges = len(row)
    row = row + row_tfidf
    col = col + col_tfidf
    node_size = train_size + vocab_size + test_size
    adj = sp.csr_matrix(
        (weight, (row, col)), shape=(node_size, node_size))
    logger.info("Sequential graph finish! Time spent {:2f} number of edges {}".format(time.time()-t, num_edges))
    return pmi_dict, adj, row, col

def gen_tfidf(corpus, word_id_map, word_doc_freq, vocab, train_size):
    row, col, weight_tfidf = [],[],[]
    vocab_size = len(vocab)
    doc_word_freq = {}
    for doc_id in range(len(corpus)):
        doc_words = corpus[doc_id]
        words = doc_words.split()
        for word in words:
            word_id = word_id_map[word]
            doc_word_str = str(doc_id) + ',' + str(word_id)
            if doc_word_str in doc_word_freq:
                doc_word_freq[doc_word_str] += 1
            else:
                doc_word_freq[doc_word_str] = 1
    
    for i in range(len(corpus)):
        doc_words = corpus[i]
        words = doc_words.split()
        doc_word_set = set()
        for word in words:
            if word in doc_word_set:
                continue
            j = word_id_map[word]
            key = str(i) + ',' + str(j)
            freq = doc_word_freq[key]
            if i < train_size:
                row.append(i)
            else:
                row.append(i + vocab_size)
            col.append(train_size + j)
            idf = log(1.0 * len(corpus) /
                    word_doc_freq[vocab[j]])
            weight_tfidf.append(freq * idf)
            doc_word_set.add(word)
    return row, col, weight_tfidf

def gen_corpus(dataset):
    input1 = os.sep.join(['data', dataset])
    doc_name_list = []
    doc_train_list = []
    doc_test_list = []

    f = open(input1 + '.txt', 'r', encoding='latin1')
    lines = f.readlines()
    for line in lines:
        doc_name_list.append(line.strip())
        temp = line.split("\t")
        if temp[1].find('test') != -1:
            doc_test_list.append(line.strip())
        elif temp[1].find('train') != -1:
            doc_train_list.append(line.strip())
    f.close()

    doc_content_list = []
    f = open(input1 + '.clean.txt', 'r')
    lines = f.readlines()
    for line in lines:
        doc_content_list.append(line.strip())
    f.close()

    train_ids = []
    for train_name in doc_train_list:
        train_id = doc_name_list.index(train_name)
        train_ids.append(train_id)
    random.shuffle(train_ids)

    train_ids_str = '\n'.join(str(index) for index in train_ids)

    test_ids = []
    for test_name in doc_test_list:
        test_id = doc_name_list.index(test_name)
        test_ids.append(test_id)
    # print(test_ids)
    random.shuffle(test_ids)

    test_ids_str = '\n'.join(str(index) for index in test_ids)

    ids = train_ids + test_ids
    # print(ids)
    # print(len(ids))

    shuffle_doc_name_list = []
    shuffle_doc_words_list = []
    for id in ids:
        shuffle_doc_name_list.append(doc_name_list[int(id)])
        shuffle_doc_words_list.append(doc_content_list[int(id)])
    label_set = set()
    for doc_meta in shuffle_doc_name_list:
        temp = doc_meta.split('\t')
        label_set.add(temp[2])
    label_list = list(label_set)
    labels = []
    for one in shuffle_doc_name_list:
        entry = one.split('\t')
        labels.append(label_list.index(entry[-1]))
    shuffle_doc_name_str = '\n'.join(shuffle_doc_name_list)
    shuffle_doc_words_str = '\n'.join(shuffle_doc_words_list)
    word_freq = {}
    word_set = set()
    for doc_words in shuffle_doc_words_list:
        words = doc_words.split()
        for word in words:
            word_set.add(word)
            if word in word_freq:
                word_freq[word] += 1
            else:
                word_freq[word] = 1

    vocab = list(word_set)
    vocab_size = len(vocab)

    word_doc_list = {}

    for i in range(len(shuffle_doc_words_list)):
        doc_words = shuffle_doc_words_list[i]
        words = doc_words.split()
        appeared = set()
        for word in words:
            if word in appeared:
                continue
            if word in word_doc_list:
                doc_list = word_doc_list[word]
                doc_list.append(i)
                word_doc_list[word] = doc_list
            else:
                word_doc_list[word] = [i]
            appeared.add(word)

    word_doc_freq = {}
    for word, doc_list in word_doc_list.items():
        word_doc_freq[word] = len(doc_list)

    word_id_map = {}
    id_word_map = {}
    for i in range(vocab_size):
        word_id_map[vocab[i]] = i
        id_word_map[i] = vocab[i]

    return shuffle_doc_name_list, shuffle_doc_words_list, train_ids, test_ids, word_doc_freq, word_id_map, id_word_map, vocab, labels, label_list

def main(args, timestamp):
    # load stanfordcorenlp
    nlp = StanfordCoreNLP(args.corenlp, lang='en')

    # generate seed for reproducability
    set_torch_seed(seed=148)

    # set gpu or cpu
    if torch.cuda.is_available():
        device = torch.device("cuda")
        logger.info("Training is running on {}".format(device))
    else:
        device = torch.device("cpu")
        logger.info("Training is running on CPU")

    # load corpus
    name, corpus, train_ids, test_ids, word_doc_freq, word_id_map, id_word_map, vocab, labels, label_list = gen_corpus(args.dataset)
    data = [train_ids, test_ids, corpus, labels, vocab, word_id_map, id_word_map, label_list]

    json.dump(data, open('./data/{}_data.json'.format(args.dataset),'w'))
    num_labels = len(label_list)

    row_tfidf, col_tfidf, weight_tfidf = gen_tfidf(corpus, word_id_map, word_doc_freq, vocab, len(train_ids))

    # create directory for saving LSTM 
    graphs_saved_path = "saved_graphs/run_{}".format(timestamp)
    argparse_dict = vars(args)
    if not os.path.exists(graphs_saved_path):
        os.makedirs(graphs_saved_path)
        with open(os.path.join(graphs_saved_path, 'graph_config.json'.format(timestamp)), 'w') as fjson:
            json.dump(argparse_dict, fjson)

    # generate sequential graph if true
    if args.gen_seq:
        pmi_dict, seq_adj, row, col = gen_seq(corpus, len(train_ids), len(test_ids), args.window_size, word_id_map, row_tfidf, col_tfidf, weight_tfidf, vocab)

        # pickle graph object
        pickle_graph(
            graph_type='sequential', 
            dataset=args.dataset, 
            graph_adj=seq_adj,
            graph_saved_path=graphs_saved_path)
    
    # generate syntatic graph if true
    if args.gen_syn:
        syn_adj = gen_syn(corpus, nlp, row_tfidf, col_tfidf, weight_tfidf, word_id_map, len(train_ids)+len(vocab)+len(test_ids), len(train_ids))

        # pickle graph object
        pickle_graph(
            graph_type='syntactic', 
            dataset=args.dataset, 
            graph_adj=syn_adj,
            graph_saved_path=graphs_saved_path)
    
    # generate syntatic graph with bidirectional weights if true
    if args.gen_syn_bidirect:
        syn_adj_bi = gen_syn_bidirect(corpus, nlp, row_tfidf, col_tfidf, weight_tfidf, word_id_map, len(train_ids)+len(vocab)+len(test_ids), len(train_ids))
         # pickle graph object
        pickle_graph(
            graph_type='syntactic', 
            dataset=args.dataset, 
            graph_adj=syn_adj_bi,
            graph_saved_path=graphs_saved_path)
    
    # generate syntatic graph with bidirectional weights and tfidf scaling if true    
    if args.gen_syn_tfidf_scaling:
        syn_adj_tfidf = gen_syn_tfidf_scaling(corpus, nlp, row_tfidf, col_tfidf, weight_tfidf, word_id_map, len(train_ids)+len(vocab)+len(test_ids), len(train_ids))
       pickle_graph(
            graph_type='syntactic', 
            dataset=args.dataset, 
            graph_adj=syn_adj_tfidf,
            graph_saved_path=graphs_saved_path)

    # generate semantic graph if true
    if args.gen_sem:
        valid_size = int(0.1*len(train_ids))
        train_size = len(train_ids) - valid_size
        sem_adj = gen_sem(args, corpus, word_id_map, row_tfidf, col_tfidf, weight_tfidf, args.thres, train_size, valid_size, labels, num_labels, len(train_ids)+len(vocab)+len(test_ids),device, graphs_saved_path)

        # pickle graph object
        pickle_graph(
            graph_type='semantic', 
            dataset=args.dataset, 
            graph_adj=sem_adj,
            graph_saved_path=graphs_saved_path)

def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description='Training and Testing Knowledge Graph Embedding Models',
        usage='train.py [<args>] [-h | --help]'
    )
    parser.add_argument('--gen_syn', action='store_true')
    parser.add_argument('--gen_syn_tfidf_scaling', action='store_true')
    parser.add_argument('--gen_syn_bidirect', action='store_true')
    parser.add_argument('--gen_sem', action='store_true')
    parser.add_argument('--gen_seq', action='store_true')
    parser.add_argument('--dataset', type=str, default='mr')
    parser.add_argument('--window_size', type=int, default=7)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--embed_size", type=int, default=200)
    parser.add_argument("--max_len", default=512, type=int)
    parser.add_argument("--hidden_size", type=int, default=200)
    parser.add_argument("--dropout", default=0, type=float)
    parser.add_argument("--weight_decay", default=1e-6, type=float)
    parser.add_argument("--epochs", default=20, type=int)
    parser.add_argument("--seed", default=32, type=int)
    parser.add_argument("--corenlp", default='./stanford-corenlp-4.5.0')
    parser.add_argument('--thres', default=0.05, type=float, help="the threshold of semantic graph")
    parser.add_argument('--lstm_layers', default=1, type=int, help="number of layers in LSTM")
    return parser.parse_args(args)

if __name__ == '__main__':
    # retrieve execution timestamp for logs
    sgt = pytz.timezone('Asia/Singapore')
    timestamp = datetime.now(sgt).strftime("%Y-%m-%d_%H-%M-%S")

    # set up logging
    log_path = os.path.join(Path(os.path.abspath(os.path.dirname(__file__)), '../logs'))
    logger = setup_logging(log_path=log_path, log_name='graph_log', log_filename='graph', timestamp=timestamp)

    main(parse_args(), timestamp)
