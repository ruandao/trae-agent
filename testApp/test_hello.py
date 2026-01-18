#!/usr/bin/env python3
"""
Test script for hello.py

This script tests that the hello.py script produces the correct output.
"""

import subprocess
import sys

def test_hello_output():
    """Test that hello.py prints 'Hello, World!'"""
    result = subprocess.run(
        [sys.executable, "hello.py"],
        capture_output=True,
        text=True
    )
    
    output = result.stdout.strip()
    expected = "Hello, World!"
    
    if output == expected:
        print("✓ Test passed: hello.py prints 'Hello, World!'")
        return True
    else:
        print(f"✗ Test failed: Expected '{expected}', got '{output}'")
        return False

def test_importable():
    """Test that hello.py can be imported without side effects"""
    try:
        # Import the module
        import hello
        
        # Check that main wasn't called during import
        # (we can't easily test this without mocking, but import should succeed)
        print("✓ Test passed: hello.py can be imported successfully")
        return True
    except Exception as e:
        print(f"✗ Test failed: Could not import hello.py: {e}")
        return False

if __name__ == "__main__":
    print("Running tests for hello.py...")
    print("-" * 40)
    
    test1_passed = test_hello_output()
    test2_passed = test_importable()
    
    print("-" * 40)
    
    if test1_passed and test2_passed:
        print("All tests passed! ✓")
        sys.exit(0)
    else:
        print("Some tests failed. ✗")
        sys.exit(1)