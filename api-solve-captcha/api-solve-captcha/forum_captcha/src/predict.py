from docopt import docopt
import torch
from tqdm import tqdm
from torch.utils.data import DataLoader

from forum_captcha.src.config import common_config as config
from forum_captcha.src.dataset import CustomDataset
from forum_captcha.src.model import CRNN
from forum_captcha.src.ctc_decoder import ctc_decode


def predict(crnn, dataloader, label2char, decode_method, beam_size):
    crnn.eval()
    pbar = tqdm(total=len(dataloader), desc="Predict")

    all_preds = []
    with torch.no_grad():
        for data in dataloader:
            device = 'cuda' if next(crnn.parameters()).is_cuda else 'cpu'

            images = data.to(device)

            logits = crnn(images)
            log_probs = torch.nn.functional.log_softmax(logits, dim=2)

            preds = ctc_decode(log_probs, method=decode_method, beam_size=beam_size,
                               label2char=label2char)
            all_preds += preds

            pbar.update(1)
        pbar.close()

    return all_preds


def show_result(paths, preds):
    print('\n===== result =====')
    for path, pred in zip(paths, preds):
        text = ''.join(pred)
        print(f'{path} > {text}')
        

def predict_image(crnn, image, label2char, decode_method, beam_size):
    crnn.eval()
    with torch.no_grad():
        device = 'cuda' if next(crnn.parameters()).is_cuda else 'cpu'

        images = image.to(device)

        logits = crnn(images)
        log_probs = torch.nn.functional.log_softmax(logits, dim=2)

        preds = ctc_decode(log_probs, method=decode_method, beam_size=beam_size,
                           label2char=label2char)
    return preds


def predict_captcha(image_path):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'device: {device}')

    img_height = config['img_height']
    img_width = config['img_width']

    num_class = len(CustomDataset.LABEL2CHAR) + 1
    crnn = CRNN(1, img_height, img_width, num_class,
                map_to_seq_hidden=config['map_to_seq_hidden'],
                rnn_hidden=config['rnn_hidden'],
                leaky_relu=config['leaky_relu'])
    crnn.load_state_dict(torch.load('./forum_captcha/checkpoints/weight.pt', map_location=device))
    crnn.to(device)

    import cv2
    image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    image = cv2.bitwise_not(image)
    manual_threshold = 150
    image = cv2.threshold(image, manual_threshold, 255, cv2.THRESH_BINARY)[1]
    image = cv2.resize(image, (100, 32), interpolation=cv2.INTER_LINEAR)
    image = (image / 127.5) - 1.0
    image = image.reshape((1, 1, 32, 100))
    image = torch.FloatTensor(image)
    preds = predict_image(crnn, image, CustomDataset.LABEL2CHAR, 'beam_search', 10)
    
    return preds


def main():
    arguments = docopt(__doc__)
    images = arguments['IMAGE']
    reload_checkpoint = arguments['-m']
    batch_size = int(arguments['-s'])
    decode_method = arguments['-d']
    beam_size = int(arguments['-b'])

    img_height = config['img_height']
    img_width = config['img_width']

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'device: {device}')

    predict_dataset = CustomDataset(paths=images,
                                      img_height=img_height, img_width=img_width)

    predict_loader = DataLoader(
        dataset=predict_dataset,
        batch_size=batch_size,
        shuffle=False)

    num_class = len(CustomDataset.LABEL2CHAR) + 1
    crnn = CRNN(1, img_height, img_width, num_class,
                map_to_seq_hidden=config['map_to_seq_hidden'],
                rnn_hidden=config['rnn_hidden'],
                leaky_relu=config['leaky_relu'])
    crnn.load_state_dict(torch.load(reload_checkpoint, map_location=device))
    crnn.to(device)

    preds = predict(crnn, predict_loader, CustomDataset.LABEL2CHAR,
                    decode_method=decode_method,
                    beam_size=beam_size)

    show_result(images, preds)
    



if __name__ == '__main__':
    main()
