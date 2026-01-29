#!/usr/bin/env python3
"""
Florence-2 Neuron Benchmark Script

Benchmarks inference performance across different tasks.
Supports single-core and dual-core (dual-process) testing.

Usage:
    # Single core benchmark
    python benchmark.py --image test.jpg
    
    # Stress test (5 minutes)
    python benchmark.py --stress --duration 300
"""

import os
import sys
import time
import argparse
import torch
import torch_neuronx
import requests
from PIL import Image
from io import BytesIO
from inference import Florence2NeuronBF16

TEST_IMAGES = [
    "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/transformers/tasks/car.jpg",
    "https://images.unsplash.com/photo-1518791841217-8f162f1e1131?w=400",
    "https://images.unsplash.com/photo-1587300003388-59208cc962cb?w=400",
]

TASKS = ["<CAPTION>", "<DETAILED_CAPTION>", "<OD>", "<OCR>"]


def download_images():
    """Download test images."""
    images = []
    for url in TEST_IMAGES:
        try:
            img = Image.open(BytesIO(requests.get(url, timeout=10).content)).convert("RGB")
            images.append(img)
        except:
            pass
    return images


def benchmark_tasks(model, image, iterations=10):
    """Benchmark all tasks."""
    print("\n=== Task Benchmark ===")
    for task in TASKS:
        times = []
        for _ in range(iterations):
            t0 = time.time()
            result = model(image, task, max_tokens=100)
            times.append((time.time() - t0) * 1000)
        avg = sum(times) / len(times)
        print(f"{task}: {avg:.0f}ms avg ({result[:50]}...)")


def stress_test(model, images, duration=300):
    """Run stress test for specified duration."""
    core_id = os.environ.get("NEURON_RT_VISIBLE_CORES", "0")
    print(f"\n[NC{core_id}] Starting {duration}s stress test...")
    
    # Warmup
    for _ in range(3):
        model(images[0], "<OD>")
    
    count = 0
    start = time.time()
    while time.time() - start < duration:
        model(images[count % len(images)], "<OD>")
        count += 1
        if count % 50 == 0:
            elapsed = time.time() - start
            print(f"[NC{core_id}] {count} reqs, {elapsed:.0f}s, QPS={count/elapsed:.2f}")
    
    total = time.time() - start
    print(f"[NC{core_id}] DONE: {count} reqs, {total:.1f}s, QPS={count/total:.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="Path to test image")
    parser.add_argument("--model-dir", default="./compiled_bf16")
    parser.add_argument("--core", default="0", help="NeuronCore ID")
    parser.add_argument("--stress", action="store_true", help="Run stress test")
    parser.add_argument("--duration", type=int, default=300, help="Stress test duration")
    args = parser.parse_args()
    
    os.environ["NEURON_RT_VISIBLE_CORES"] = args.core
    os.environ["NEURON_RT_NUM_CORES"] = "1"
    
    model = Florence2NeuronBF16(args.model_dir, args.core)
    images = download_images()
    
    if args.stress:
        stress_test(model, images, args.duration)
    else:
        image = Image.open(args.image) if args.image else images[0]
        benchmark_tasks(model, image)


if __name__ == "__main__":
    main()
