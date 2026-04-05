# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Simple CLI Console implementation."""

import asyncio
import os
import shutil
import sys
from typing import override

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from trae_agent.agent.agent_basics import AgentExecution, AgentState, AgentStep, AgentStepState
from trae_agent.utils.cli.cli_console import (
    AGENT_STATE_INFO,
    CLIConsole,
    ConsoleMode,
    ConsoleStep,
    generate_agent_step_table,
)
from trae_agent.utils.config import LakeviewConfig

# 与 onlineService 子进程默认一致：Rich 无「无限宽」，用大整数近似不折行。
_WIDE_CONSOLE_FALLBACK = 999_999


def _simple_console_width() -> int | None:
    """Rich 在非 TTY 上默认 80 列；优先用环境 COLUMNS（正整数），否则用大宽度近似不限制折行。"""
    cols = os.environ.get("COLUMNS")
    if cols:
        s = cols.strip()
        if s.isdigit():
            n = int(s)
            if n > 0:
                return None
    try:
        if sys.stdout.isatty():
            return None
    except (AttributeError, ValueError, OSError):
        pass
    return _WIDE_CONSOLE_FALLBACK


class SimpleCLIConsole(CLIConsole):
    """Simple text-based CLI console that prints agent execution trace."""

    def __init__(
        self, mode: ConsoleMode = ConsoleMode.RUN, lakeview_config: LakeviewConfig | None = None
    ):
        """Initialize the simple CLI console.

        Args:
            config: Configuration object containing lakeview and other settings
            mode: Console operation mode
        """
        super().__init__(mode, lakeview_config)
        cw = _simple_console_width()
        self.console: Console = Console(width=cw) if cw is not None else Console()

    @override
    def update_status(
        self, agent_step: AgentStep | None = None, agent_execution: AgentExecution | None = None
    ):
        """Update the console status with new agent step or execution info."""
        if agent_step:
            if agent_step.step_number not in self.console_step_history:
                # update step history
                self.console_step_history[agent_step.step_number] = ConsoleStep(agent_step)

            if (
                agent_step.state in [AgentStepState.COMPLETED, AgentStepState.ERROR]
                and not self.console_step_history[agent_step.step_number].agent_step_printed
            ):
                self._print_step_update(agent_step, agent_execution)
                self.console_step_history[agent_step.step_number].agent_step_printed = True

                # If lakeview is enabled, generate lakeview panel in the background
                if (
                    self.lake_view
                    and not self.console_step_history[
                        agent_step.step_number
                    ].lake_view_panel_generator
                ):
                    self.console_step_history[
                        agent_step.step_number
                    ].lake_view_panel_generator = asyncio.create_task(
                        self._create_lakeview_step_display(agent_step)
                    )

        self.agent_execution = agent_execution

    @override
    async def start(self):
        """Start the console - wait for completion and then print summary."""
        while self.agent_execution is None or (
            self.agent_execution.agent_state != AgentState.COMPLETED
            and self.agent_execution.agent_state != AgentState.ERROR
        ):
            await asyncio.sleep(1)

        # Print lakeview summary if enabled
        if self.lake_view and self.agent_execution:
            await self._print_lakeview_summary()

        # Print execution summary
        if self.agent_execution:
            self._print_execution_summary()

    def _print_step_update(
        self, agent_step: AgentStep, agent_execution: AgentExecution | None = None
    ):
        """Print a step update as it progresses."""

        table = generate_agent_step_table(agent_step)

        if agent_step.llm_usage:
            table.add_row(
                "Token Usage",
                f"Input: {agent_step.llm_usage.input_tokens} Output: {agent_step.llm_usage.output_tokens}",
            )

        if agent_execution and agent_execution.total_tokens:
            table.add_row(
                "Total Tokens",
                f"Input: {agent_execution.total_tokens.input_tokens} Output: {agent_execution.total_tokens.output_tokens}",
            )

        self.console.print(table)

    async def _print_lakeview_summary(self):
        """Print lakeview summary of all completed steps."""
        self.console.print("\n" + "=" * 60)
        self.console.print("[bold cyan]Lakeview Summary[/bold cyan]")
        self.console.print("=" * 60)

        for step in self.console_step_history.values():
            if step.lake_view_panel_generator:
                lake_view_panel = await step.lake_view_panel_generator
                if lake_view_panel:
                    self.console.print(lake_view_panel)

    def _print_execution_summary(self):
        """Print the final execution summary."""
        if not self.agent_execution:
            return

        self.console.print("\n" + "=" * 60)
        self.console.print("[bold green]Execution Summary[/bold green]")
        self.console.print("=" * 60)

        # Create summary table（不限制宽度，避免长任务描述被折行或省略号截断）
        table = Table(show_header=False)
        table.add_column("Metric", style="cyan", no_wrap=True)
        table.add_column("Value", style="green", no_wrap=True, overflow="ignore")

        table.add_row("Task", self.agent_execution.task)
        table.add_row("Success", "✅ Yes" if self.agent_execution.success else "❌ No")
        table.add_row("Steps", str(len(self.agent_execution.steps)))
        table.add_row("Execution Time", f"{self.agent_execution.execution_time:.2f}s")

        if self.agent_execution.total_tokens:
            total_tokens = (
                self.agent_execution.total_tokens.input_tokens
                + self.agent_execution.total_tokens.output_tokens
            )
            table.add_row("Total Tokens", str(total_tokens))
            table.add_row("Input Tokens", str(self.agent_execution.total_tokens.input_tokens))
            table.add_row("Output Tokens", str(self.agent_execution.total_tokens.output_tokens))

        self.console.print(table)

        # Display final result
        if self.agent_execution.final_result:
            self.console.print(
                Panel(
                    Markdown(self.agent_execution.final_result),
                    title="Final Result",
                    border_style="green" if self.agent_execution.success else "red",
                )
            )

    @override
    def print_task_details(self, details: dict[str, str]):
        """Print initial task configuration details."""
        renderable = ""
        for key, value in details.items():
            renderable += f"[bold]{key}:[/bold] {value}\n"
        renderable = renderable.strip()
        self.console.print(
            Panel(
                renderable,
                title="Task Details",
                border_style="blue",
            )
        )

    @override
    def print(self, message: str, color: str = "blue", bold: bool = False):
        """Print a message to the console."""
        message = f"[bold]{message}[/bold]" if bold else message
        message = f"[{color}]{message}[/{color}]"
        self.console.print(message)

    @override
    def get_task_input(self) -> str | None:
        """Get task input from user (for interactive mode)."""
        if self.mode != ConsoleMode.INTERACTIVE:
            return None

        self.console.print("\n[bold blue]Task:[/bold blue] ", end="")
        try:
            task = input()
            if task.lower() in ["exit", "quit"]:
                return None
            return task
        except (EOFError, KeyboardInterrupt):
            return None

    @override
    def get_working_dir_input(self) -> str:
        """Get working directory input from user (for interactive mode)."""
        if self.mode != ConsoleMode.INTERACTIVE:
            return ""

        self.console.print("[bold blue]Working Directory:[/bold blue] ", end="")
        try:
            return input()
        except (EOFError, KeyboardInterrupt):
            return ""

    @override
    def stop(self):
        """Stop the console and cleanup resources."""
        # Simple console doesn't need explicit cleanup
        pass

    async def _create_lakeview_step_display(self, agent_step: AgentStep) -> Panel | None:
        """Create lakeview display for a step."""
        if self.lake_view is None:
            return None

        lake_view_step = await self.lake_view.create_lakeview_step(agent_step)

        if lake_view_step is None:
            return None

        color, _ = AGENT_STATE_INFO.get(agent_step.state, ("white", "❓"))

        return Panel(
            f"""[{lake_view_step.tags_emoji}] The agent [bold]{lake_view_step.desc_task}[/bold]
{lake_view_step.desc_details}""",
            title=f"Step {agent_step.step_number} (Lakeview)",
            border_style=color,
            width=80,
        )
