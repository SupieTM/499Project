import paddleocr
import paddle
from PIL import Image, ImageDraw
import numpy as np
import json
import re
import sys


class idIdent:
    ocr = paddleocr.PaddleOCR(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False
    )

    dataSet = []

    # regex

    toyotaRegex = "TOYOTA"
    firstID = "([A-z0-9]{2}-[A-z0-9]{5})|[A-z]"
    secondID = "[A-z0-9]{5}(\\.[A-z0-9])?-[A-z0-9]{4}-[A-z0-9]{2}"
    thirdID = "[A-Z]{3}[0-9]{3}"
    fourthID = "[0-9]{4}-[0-9]{2}"

    regexes = [toyotaRegex, firstID, secondID, thirdID, fourthID]

    def __init__(self):
        pass

    def parseImg(self, img):

        imgArray = np.array(img.convert("RGB"))

        result = self.ocr.predict(imgArray)

        self.dataSet.clear()

        for res in result:
            data = res.json

            self.dataSet.append(data)

    def readJsons(self):

        for data in self.dataSet:
            print(json.dumps(data, indent=4))

    def readText(self):

        for data in self.dataSet:
            print(data["res"]["rec_texts"])

    def retTextwConf(self, conf):

        retList = ["_", "_", "_", "_", "_"]
        confList = [0, 0, 0, 0, 0]

        for data in self.dataSet:
            for index, value in enumerate(data["res"]["rec_scores"]):

                if value < conf:
                    print("continuing")
                    continue

                string = data["res"]["rec_texts"][index]

                useMatch = None
                useIndex = -1

                for index2, regex in enumerate(self.regexes):

                    match = re.search(regex, string)

                    if not match:
                        continue

                    if (useMatch):
                        if (len(useMatch.group()) >= len(match.group())):
                            continue

                    if (len(retList[index2]) < len(match.group())):
                        useMatch = match
                        useIndex = index2

                if useIndex >= 0:
                    retList[useIndex] = useMatch.group()
                    confList[useIndex] = value

        return confList, retList

    def drawBoundingBoxes(self, img):

        draw = ImageDraw.Draw(img)

        for result in self.dataSet:
            for index, value in enumerate(result["res"]["rec_texts"]):

                boxPoints = result["res"]["rec_boxes"][index]

                lowerLeft = (boxPoints[0], boxPoints[1])
                upperRight = (boxPoints[2], boxPoints[3])

                draw.rectangle([lowerLeft, upperRight], outline="green", width=3)
                draw.text((lowerLeft[0], lowerLeft[1] - 30), value, fill="red", align="center", font_size=28)

        img.show("Complete_Num.png")


def main():
    img = Image.open(sys.argv[1])

    orc = idIdent()

    orc.parseImg(img)

    orc.drawBoundingBoxes(img)

    orc.readJsons()

    conf, retList = orc.retTextwConf(0.0)

    print(conf, retList)


if __name__ == "__main__":
    main()
