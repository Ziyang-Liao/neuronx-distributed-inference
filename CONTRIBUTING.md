# Contributing to Florence-2 on AWS Inferentia2

Thank you for your interest in contributing!

## How to Contribute

### Reporting Issues

- Search existing issues before creating a new one
- Include Neuron SDK version, instance type, and error logs
- Provide minimal reproduction steps

### Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes
4. Test on an Inferentia2 instance
5. Submit a pull request

### Code Style

- Follow PEP 8 for Python code
- Add docstrings for public functions
- Include type hints where possible

### Testing

```bash
# Run tests
python -m pytest tests/

# Test inference
python -m models.florence2_bf16.inference --image test.jpg
```

## Development Setup

```bash
# Clone
git clone https://github.com/Ziyang-Liao/neuronx-distributed-inference.git
cd neuronx-distributed-inference

# Install dependencies
pip install -r requirements.txt

# Compile models (requires Inferentia2)
python -m models.florence2_bf16.compile --output-dir ./compiled_bf16
```

## Areas for Contribution

- Additional model support (other vision-language models)
- Performance optimizations
- Documentation improvements
- Bug fixes

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
