#!/usr/bin/env python3
"""
Quick Start Example for Florence-2 on Inferentia2

This script demonstrates end-to-end usage:
1. Compile models (if not already compiled)
2. Run inference on sample images
3. Display results

Usage:
    python examples/quick_start.py --image your_image.jpg
    python examples/quick_start.py --compile  # Force recompilation
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def download_sample_image():
    """Download a sample image for testing."""
    import requests
    url = "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/transformers/tasks/car.jpg"
    path = "/tmp/sample_car.jpg"
    if not os.path.exists(path):
        print("Downloading sample image...")
        r = requests.get(url)
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def compile_models(output_dir: str):
    """Compile BF16 models."""
    from models.florence2_bf16.compile import main as compile_main
    print(f"Compiling models to {output_dir}...")
    sys.argv = ["compile.py", "--output-dir", output_dir]
    compile_main()


def run_inference(model_dir: str, image_path: str):
    """Run inference on an image."""
    from models.florence2_bf16.inference import Florence2NeuronBF16
    
    print(f"\nLoading model from {model_dir}...")
    model = Florence2NeuronBF16(model_dir, core_id="0")
    
    print(f"Running inference on {image_path}...\n")
    
    tasks = [
        ("<CAPTION>", "Caption"),
        ("<DETAILED_CAPTION>", "Detailed Caption"),
        ("<OD>", "Object Detection"),
        ("<OCR>", "OCR"),
    ]
    
    for prompt, name in tasks:
        start = time.time()
        result = model(image_path, prompt)
        elapsed = (time.time() - start) * 1000
        print(f"[{name}] ({elapsed:.0f}ms)")
        print(f"  {result}\n")


def main():
    parser = argparse.ArgumentParser(description="Florence-2 Quick Start")
    parser.add_argument("--image", type=str, help="Path to input image")
    parser.add_argument("--model-dir", type=str, default="./compiled_bf16",
                        help="Directory for compiled models")
    parser.add_argument("--compile", action="store_true",
                        help="Force model compilation")
    args = parser.parse_args()
    
    # Check if models exist
    model_dir = args.model_dir
    need_compile = args.compile or not os.path.exists(f"{model_dir}/stage0.pt")
    
    if need_compile:
        compile_models(model_dir)
    
    # Get image path
    image_path = args.image or download_sample_image()
    
    # Run inference
    run_inference(model_dir, image_path)


if __name__ == "__main__":
    main()
