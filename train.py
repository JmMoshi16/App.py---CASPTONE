# -*- coding: utf-8 -*-
# train.py - ECOReward Bin Custom Model Trainer
# Run this after you have photos in dataset/images/train and dataset/images/val

from ultralytics import YOLO
import os

print("------------------------------------------")
print("  ECOReward Bin - YOLOv8 Custom Trainer")
print("------------------------------------------")

# Check dataset exists (both train and validation)
if not os.path.exists("dataset/train/images"):
    print("[ERROR] dataset/train/images folder not found!")
    print("  Run python download_dataset.py first.")
    exit()

if not os.path.exists("dataset/valid/images"):
    print("[ERROR] dataset/valid/images folder not found!")
    print("  Run python download_dataset.py first.")
    exit()

train_count = len(os.listdir("dataset/train/images"))
val_count   = len(os.listdir("dataset/valid/images"))
print(f"[INFO] Train images : {train_count}")
print(f"[INFO] Val images   : {val_count}")

if train_count < 10:
    print("[WARNING] Very few training images. Aim for 50+ per class.")

# Load base model (pretrained on COCO — good starting point)
model = YOLO("yolov8n.pt")

print("\n[TRAINING] Starting... this may take a while.")
print("  Classes: BIODEGRADABLE, CARDBOARD, GLASS, METAL, PAPER, PLASTIC\n")

results = model.train(
    data    = "dataset/data.yaml",
    epochs  = 50,
    imgsz   = 320,
    batch   = 4,
    amp     = False,
    name    = "ecoreward_cafe",
    patience= 5,
    device  = 'cpu',
    workers = 0,
)

print("\n[DONE] Training complete!")
print(f"  Best model saved to: runs/detect/ecoreward_cafe/weights/best.pt")
print("  Copy best.pt to your ECOReward_Capstone2 folder to use it in app.py")
