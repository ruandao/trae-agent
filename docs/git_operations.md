# Git Operations in Trae Agent

## Issue: Git Clone Timeouts

Trae Agent's bash tool has a default timeout of 120 seconds per command. This can be insufficient for large git operations such as:
- Cloning large repositories
- Fetching from repositories with extensive history
- Operations over slow network connections

## Solution: Configurable Timeout

The timeout can now be configured via the `TRAE_BASH_TOOL_TIMEOUT` environment variable.

### Example Usage

```bash
# Set a longer timeout for git operations (5 minutes)
export TRAE_BASH_TOOL_TIMEOUT=300.0

# Run trae-cli with longer timeout for git clone
trae-cli run "Clone the repository from https://github.com/user/repo"
```

### Alternative Approach: Background Execution

For very long-running operations, you can run commands in the background:

```bash
# Example: Clone in background and check later
git clone https://github.com/user/repo.git /path/to/clone &
# Check if clone completed
wait %1 || echo "Clone may have failed"
```

## Best Practices for Git Operations

1. **Use `--depth 1` for shallow clones** when you only need recent history:
   ```bash
   git clone --depth 1 https://github.com/user/repo.git
   ```

2. **Use SSH instead of HTTPS** if you have SSH keys configured:
   ```bash
   git clone git@github.com:user/repo.git
   ```

3. **Check repository size first** if possible:
   ```bash
   git ls-remote git@github.com:user/repo.git
   ```

4. **Handle credentials properly** - ensure git credential helpers are configured.

## Troubleshooting

### Common Issues

1. **"timed out: bash has not returned in 120 seconds"**
   - Set `TRAE_BASH_TOOL_TIMEOUT` to a higher value
   - Consider using background execution

2. **Authentication failures**
   - Ensure SSH keys are configured for SSH URLs
   - For HTTPS, configure git credential helper
   - Use `GIT_SSH_COMMAND` environment variable for SSH options

3. **Network issues**
   - Git operations may fail or timeout on slow connections
   - Consider cloning with `--depth` to reduce data transfer

### Example Script for Large Clones

```bash
#!/bin/bash
# Clone large repository with extended timeout
export TRAE_BASH_TOOL_TIMEOUT=600.0  # 10 minutes

# Or use background execution approach
git clone --depth 1 https://github.com/large/repo.git /tmp/clone_dir &
CLONE_PID=$!

# Wait with timeout
timeout 300 wait $CLONE_PID
if [ $? -eq 124 ]; then
    echo "Clone is taking longer than expected, continuing in background"
    # Optionally check progress later
fi
```

## Implementation Details

The timeout is configured in `trae_agent/tools/bash_tool.py`:

```python
class _BashSession:
    # ...
    def __init__(self) -> None:
        # ...
        # Allow timeout to be configured via environment variable
        self._timeout = float(os.environ.get("TRAE_BASH_TOOL_TIMEOUT", "120.0"))
```

The tool description has been updated to document this configuration option.