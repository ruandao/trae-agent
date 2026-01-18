# Hello World Python Script

A simple Python script that prints "Hello, World!" to the console.

## Files

- `hello.py` - The main hello world script
- `test_hello.py` - Test script to verify functionality

## Requirements

- Python 3.6 or higher

## Usage

### Run directly with Python:
```bash
python hello.py
```
or
```bash
python3 hello.py
```

### Run as an executable (Unix/Linux/Mac):
```bash
chmod +x hello.py  # Make executable (first time only)
./hello.py
```

### Import as a module:
```python
import hello
# The module can be imported without side effects
# To run the main function:
hello.main()
```

### Run tests:
```bash
python3 test_hello.py
```

## Script Structure

The script follows Python best practices:
- Uses a shebang line for portability (`#!/usr/bin/env python3`)
- Includes docstrings for documentation
- Defines a `main()` function for clarity
- Uses `if __name__ == "__main__":` guard to prevent execution when imported

## License

This is a simple example script for educational purposes.