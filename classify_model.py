# -*- coding:utf-8 -*-
import os

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "00"
from tqdm import tqdm
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from transformers import BertModel, BertConfig, AdamW, get_cosine_schedule_with_warmup
from sklearn.metrics import accuracy_score, classification_report
from dataset import SentenceDataset
import warnings, time

warnings.filterwarnings('ignore')
plm_path = 'chinese-roberta-wwm-ext'  # 该文件夹下存放三个文件（'vocab.txt', 'pytorch_model.bin', 'config.json'）
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
writer = SummaryWriter('runs/ClassifyModel2.0')

### 1. 这种方式是不需要手动下载模型文件，在网速快的时候使用
# tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
# bert = BertModel.from_pretrained('bert-base-uncased')
### 2. 这种方式是需要手动下载模型文件，网速慢使用，推荐使用第2种。
# tokenizer = BertTokenizer.from_pretrained('E:/Projects/bert-base-uncased/bert-base-uncased-vocab.txt')
# 加载bert模型，这个路径文件夹下有bert_config.json配置文件和model.bin模型权重文件
# bert = BertModel.from_pretrained('chinese-roberta-wwm-ext')

train_dataset = SentenceDataset(data_path='data/classify_data/train_data.json')
valid_dataset = SentenceDataset(data_path='data/classify_data/valid_data.json')
test_dataset = SentenceDataset(data_path='data/classify_data/test_data.json')


class SentenceClassifyModel(nn.Module):
    def __init__(self, bert_path, classes=2):
        super(SentenceClassifyModel, self).__init__()
        self.config = BertConfig.from_pretrained(bert_path)  # 导入模型超参数
        self.bert = BertModel.from_pretrained(bert_path)  # 加载预训练模型权重
        self.dropout = nn.Dropout(self.config.hidden_dropout_prob)
        self.classifier = nn.Linear(self.config.hidden_size, classes)  # 直接分类

    def forward(self, input_ids, attention_mask, token_type_ids=None):
        outputs = self.bert(input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)
        out_pool = outputs[1]  # 池化后的输出 [bs, config.hidden_size]
        logit = self.classifier(out_pool)  # [bs, classes]
        return logit


class ModelTrainer(object):
    def __init__(self, model, epochs, batch_size):
        self.model = model
        self.epochs = epochs
        self.best_model_path = "model/best_model.pth"
        self.most_model_path = "model/most_model.pth"
        self.best_acc = 0.0
        self.train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=0,
                                       pin_memory=True)  # num_workers=4, pin_memory=True 内存充足时使用，可以加速///1080只能设置num_works=0
        self.valid_loader = DataLoader(valid_dataset, batch_size=batch_size, num_workers=0, pin_memory=True)
        self.test_loader = DataLoader(test_dataset, batch_size=batch_size, num_workers=0, pin_memory=True)
        self.steps = len(self.train_loader)
        self.get_parameter_number()
        self.model_visual()

    def get_parameter_number(self):
        # 打印模型参数量
        total_num = sum(p.numel() for p in self.model.parameters())
        trainable_num = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        print(f'Total parameters: {total_num}, Trainable parameters: {trainable_num}')

    def model_train(self):
        # 模型训练
        optimizer = AdamW(self.model.parameters(), lr=2e-5, weight_decay=1e-4)  # AdamW优化器
        ## 学习率先线性warmup一个epoch，然后cosine式下降。这里给个小提示，一定要加warmup（学习率从0慢慢升上去），要不然你把它去掉试试，基本上收敛不了。
        scheduler = get_cosine_schedule_with_warmup(optimizer, num_warmup_steps=self.steps,
                                                    num_training_steps=(self.epochs) * (self.steps))
        for i in range(self.epochs):
            start_time = time.time()
            self.model.train()
            epoch_index = i + 1
            print(f"***** Running training epoch {epoch_index} *****")
            train_loss_sum = 0.0
            train_accuracy = 0.0
            batch_index = 0
            for batch in self.train_loader:
                batch_index += 1
                input_ids = batch['input_ids'].to(device)
                token_type_ids = batch['token_type_ids'].to(device)
                attention_mask = batch['attention_mask'].to(device)
                true_labels = batch['labels'].to(device)
                pred_labels = self.model(input_ids=input_ids, attention_mask=attention_mask)
                loss = nn.CrossEntropyLoss()(pred_labels, true_labels)
                optimizer.zero_grad()  # 先将梯度归零,如果不将梯度清零的话，梯度会与上一个batch的数据相关，因此该函数要写在反向传播和梯度下降之前
                loss.backward()  # 然后反向传播计算得到每个参数的梯度值
                optimizer.step()  # 最后通过梯度下降执行一步参数更新
                scheduler.step()  # 学习率变化
                # 损失和准确率的计算
                train_loss_sum += loss.item()
                train_accuracy += self.model_evaluate(true_labels, pred_labels)
                # if (batch_index) % (self.steps // 50) == 0:  # 只打印五次结果
                print('\r',
                      "Epoch {:04d} | Step {:04d}/{:04d} | Train Cost Time {:.1f}s | End Time {:.1f}s | Step Time {:.1f}s | Loss {:.4f} | Batch Accuracy {:.4f} | LR {}"
                      .format(epoch_index, batch_index, self.steps, time.time() - start_time,
                              (time.time() - start_time) / batch_index * (self.steps - batch_index),
                              (time.time() - start_time) / batch_index,
                              train_loss_sum / batch_index, train_accuracy / batch_index,
                              optimizer.state_dict()['param_groups'][0]['lr']),
                      end='', flush=True)
                writer.add_scalar(tag='loss', scalar_value=train_loss_sum / batch_index,
                                  global_step=(epoch_index - 1) * self.steps + batch_index)
                writer.add_scalar(tag='training accuracy', scalar_value=train_accuracy / batch_index,
                                  global_step=(epoch_index - 1) * self.steps + batch_index)
                writer.add_scalar(tag='learning rate', scalar_value=optimizer.state_dict()['param_groups'][0]['lr'],
                                  global_step=(epoch_index - 1) * self.steps + batch_index)
                # print('\n', "Learning rate = {}".format(optimizer.state_dict()['param_groups'][0]['lr']), end='',
                #       flush=True)
            print('\n')
            # 验证
            acc = self.model_valid(data_loader=self.valid_loader, desc='Validing.......')
            writer.add_scalar(tag='valid accuracy', scalar_value=acc,
                              global_step=epoch_index)
            if acc > self.best_acc:
                self.best_acc = acc
                torch.save(self.model.state_dict(), self.best_model_path)
            torch.save(self.model.state_dict(), self.most_model_path)
            print("\n current val_acc is {:.4f}, best val_acc is {:.4f}".format(acc, self.best_acc))
            print("Train and Valid Time {:.1f}s \n".format(time.time() - start_time))

    def model_valid(self, data_loader, desc):
        # 返回准确率   #以及真实标签和预测标签
        self.model.eval()
        true_labels, pred_labels = [], []
        with torch.no_grad():
            for batch in tqdm(data_loader, desc=desc):
                input_ids = batch['input_ids'].to(device)
                attention_mask = batch['attention_mask'].to(device)
                y_true = batch['labels'].to(device)
                y_pred = self.model(input_ids=input_ids, attention_mask=attention_mask)
                true_labels.extend(y_true.cpu().numpy())
                pred_labels.extend(torch.argmax(y_pred, dim=1).detach().cpu().numpy())
        return accuracy_score(true_labels, pred_labels)

    def model_evaluate(self, y_true, y_pred):
        # 评测函数，计算准确率
        # 转到cpu上，numpy
        np_y_true = y_true.cpu().numpy()
        np_y_pred = torch.argmax(y_pred, dim=1).detach().cpu().numpy()
        return accuracy_score(np_y_true, np_y_pred)

    def model_test(self):
        self.model.load_state_dict(torch.load(self.best_model_path))
        self.model.eval()
        true_labels, pred_labels = [], []
        with torch.no_grad():
            for batch in tqdm(self.test_loader, desc='Testing.......'):
                input_ids = batch['input_ids'].to(device)
                attention_mask = batch['attention_mask'].to(device)
                y_true = batch['labels'].to(device)
                y_pred = self.model(input_ids=input_ids, attention_mask=attention_mask)
                true_labels.extend(y_true.cpu().numpy())
                pred_labels.extend(torch.argmax(y_pred, dim=1).detach().cpu().numpy())
        print("\n Test Accuracy = {} \n".format(accuracy_score(true_labels, pred_labels)))
        print(classification_report(true_labels, pred_labels, digits=2))

    def model_visual(self):  # tensorboard可视化
        sample = next(iter(self.train_loader))
        writer.add_graph(self.model,
                         input_to_model=(sample['input_ids'].to(device), sample['attention_mask'].to(device)))


classify_model = SentenceClassifyModel(plm_path).to(device)
classify_model_train = ModelTrainer(model=classify_model, epochs=2, batch_size=4)
classify_model_train.model_train()
classify_model_train.model_test()

# classification_report
'''精确度：precision，正确预测为正的，占全部预测为正的比例，TP / (TP+FP)

召回率：recall，正确预测为正的，占全部实际为正的比例，TP / (TP+FN)

F1-score：精确率和召回率的调和平均数，2 * precision*recall / (precision+recall)

support:当前行的类别在测试数据中的样本总量

同时还会给出总体的微平均值，宏平均值和加权平均值。

accuracy：计算正确率 (TP+TN) / (TP＋FP＋FN＋TN)

macro avg：各类的precision，recall，f1加和求平均

weighted avg :对每一类别的f1_score进行加权平均，权重为各类别数在y_true中所占比例
'''