from .src.predict import predict_captcha

def process_captcha(image_path):
    pred = predict_captcha(image_path)
    text = ''.join(pred[0])
    return text


if __name__ == '__main__':
    image_path = "E:/captcha/ocr_captcha/data/images/image_70.png"
    text = process_captcha(image_path)
    print(text)