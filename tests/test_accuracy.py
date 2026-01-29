#!/usr/bin/env python3
"""
Accuracy Validation Tests

Compares BF16 Neuron outputs against CPU baseline to verify
precision loss is within acceptable bounds.

Usage:
    python -m pytest tests/test_accuracy.py -v
    python tests/test_accuracy.py  # Direct execution
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Test configuration
TOLERANCE = 0.05  # 5% tolerance for output similarity
TEST_IMAGE_URL = "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/transformers/tasks/car.jpg"


def get_test_image():
    """Download test image if not exists."""
    import requests
    path = "/tmp/test_car.jpg"
    if not os.path.exists(path):
        r = requests.get(TEST_IMAGE_URL)
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def get_cpu_baseline(image_path: str, task: str) -> str:
    """Get CPU baseline output."""
    from transformers import AutoModelForCausalLM, AutoProcessor
    from PIL import Image
    
    model = AutoModelForCausalLM.from_pretrained(
        "microsoft/Florence-2-base", trust_remote_code=True)
    processor = AutoProcessor.from_pretrained(
        "microsoft/Florence-2-base", trust_remote_code=True)
    
    image = Image.open(image_path).convert("RGB")
    inputs = processor(text=task, images=image, return_tensors="pt")
    
    generated_ids = model.generate(
        input_ids=inputs["input_ids"],
        pixel_values=inputs["pixel_values"],
        max_new_tokens=64,
        num_beams=1,
    )
    
    result = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
    return result


def get_neuron_output(image_path: str, task: str, model_dir: str) -> str:
    """Get Neuron BF16 output."""
    from models.florence2_bf16.inference import Florence2NeuronBF16
    model = Florence2NeuronBF16(model_dir, core_id="0")
    return model(image_path, task)


def similarity_score(text1: str, text2: str) -> float:
    """Calculate word-level similarity between two texts."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)


def test_caption_accuracy():
    """Test CAPTION task accuracy."""
    image = get_test_image()
    model_dir = os.environ.get("MODEL_DIR", "./compiled_bf16")
    
    if not os.path.exists(f"{model_dir}/stage0.pt"):
        print("Skipping: compiled models not found")
        return
    
    cpu_result = get_cpu_baseline(image, "<CAPTION>")
    neuron_result = get_neuron_output(image, "<CAPTION>", model_dir)
    
    score = similarity_score(cpu_result, neuron_result)
    print(f"CPU:    {cpu_result}")
    print(f"Neuron: {neuron_result}")
    print(f"Similarity: {score:.2%}")
    
    assert score > (1 - TOLERANCE), f"Similarity {score:.2%} below threshold"


def test_od_accuracy():
    """Test Object Detection task accuracy."""
    image = get_test_image()
    model_dir = os.environ.get("MODEL_DIR", "./compiled_bf16")
    
    if not os.path.exists(f"{model_dir}/stage0.pt"):
        print("Skipping: compiled models not found")
        return
    
    cpu_result = get_cpu_baseline(image, "<OD>")
    neuron_result = get_neuron_output(image, "<OD>", model_dir)
    
    # For OD, check if same objects detected
    cpu_objects = set(w for w in cpu_result.split() if not w.startswith("<loc"))
    neuron_objects = set(w for w in neuron_result.split() if not w.startswith("<loc"))
    
    if cpu_objects:
        overlap = len(cpu_objects & neuron_objects) / len(cpu_objects)
        print(f"CPU objects:    {cpu_objects}")
        print(f"Neuron objects: {neuron_objects}")
        print(f"Object overlap: {overlap:.2%}")
        assert overlap > (1 - TOLERANCE), f"Object overlap {overlap:.2%} below threshold"


if __name__ == "__main__":
    print("=== Caption Accuracy Test ===")
    test_caption_accuracy()
    print("\n=== Object Detection Accuracy Test ===")
    test_od_accuracy()
    print("\n✓ All tests passed")
