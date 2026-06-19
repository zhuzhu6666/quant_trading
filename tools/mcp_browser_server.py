#!/usr/bin/env python3
"""
MCP Browser Server — Playwright browser automation for Claude Code.
Provides: navigate, click, type, screenshot, evaluate, scroll, hover,
          select, get_attribute, pdf, press_key, get_text, get_content,
          new_page, switch_page, list_pages, wait_for_selector, fill_form.

Usage: configure in ~/.claude/settings.json:
{
  "mcpServers": {
    "browser": {
      "command": "C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe",
      "args": ["tools/mcp_browser_server.py"],
      "env": { "BROWSER_HEADED": "false" }
    }
  }
}

or for headed mode:
  "env": { "BROWSER_HEADED": "true" }
"""

import asyncio
import base64
import os
import sys
from io import BytesIO
from pathlib import Path
from typing import Optional, Any

# Ensure project root on path for relative imports if needed
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
from pydantic import AnyUrl
import mcp.server.stdio

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ─── State ──────────────────────────────────────────────────────────────────
playwright_instance = None
browser: Optional[Browser] = None
context: Optional[BrowserContext] = None
page: Optional[Page] = None
pages: dict[str, Page] = {}
current_page_id: Optional[str] = None

HEADLESS = os.environ.get("BROWSER_HEADED", "false").lower() != "true"

server = Server("browser-mcp")


# ─── Browser lifecycle ──────────────────────────────────────────────────────

async def ensure_browser():
    global playwright_instance, browser, context, page, current_page_id

    if playwright_instance is None:
        playwright_instance = await async_playwright().start()
        browser = await playwright_instance.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        pages["default"] = page
        current_page_id = "default"


async def cleanup():
    global browser, playwright_instance
    if browser:
        await browser.close()
    if playwright_instance:
        await playwright_instance.stop()


def get_active_page(page_id: Optional[str] = None) -> Page:
    global current_page_id
    if page_id is None:
        page_id = current_page_id
    if page_id not in pages:
        raise ValueError(f"Page not found: {page_id}")
    return pages[page_id]


# ─── Resources ──────────────────────────────────────────────────────────────

@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    resources = []
    if pages:
        for pid, p in pages.items():
            resources.append(
                types.Resource(
                    uri=AnyUrl(f"screenshot://{pid}"),
                    name=f"Screenshot: {p.url}",
                    description=f"Current screenshot of page at {p.url}",
                    mimeType="image/png",
                )
            )
    return resources


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> bytes:
    if uri.scheme != "screenshot":
        raise ValueError(f"Unsupported URI scheme: {uri.scheme}")
    page_id = uri.host
    if page_id not in pages:
        raise ValueError(f"Page not found: {page_id}")
    return await pages[page_id].screenshot()


# ─── Tools ──────────────────────────────────────────────────────────────────

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="navigate",
            description="Navigate to a URL",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="click",
            description="Click on an element by CSS selector",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["selector"],
            },
        ),
        types.Tool(
            name="type",
            description="Type text into an input element (clears existing first via fill)",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of input"},
                    "text": {"type": "string", "description": "Text to type"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["selector", "text"],
            },
        ),
        types.Tool(
            name="get_text",
            description="Get text content from an element",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["selector"],
            },
        ),
        types.Tool(
            name="get_page_content",
            description="Get the full page HTML content",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
            },
        ),
        types.Tool(
            name="take_screenshot",
            description="Take a screenshot of the current page or element",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Optional page ID"},
                    "selector": {"type": "string", "description": "CSS selector to screenshot specific element"},
                    "full_page": {"type": "boolean", "description": "Capture full scrollable page"},
                },
            },
        ),
        types.Tool(
            name="evaluate",
            description="Execute JavaScript in the page and return the result",
            inputSchema={
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "JavaScript code to execute"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["script"],
            },
        ),
        types.Tool(
            name="scroll",
            description="Scroll the page by x,y pixels",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Horizontal scroll (default 0)"},
                    "y": {"type": "number", "description": "Vertical scroll (default 0)"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
            },
        ),
        types.Tool(
            name="hover",
            description="Hover over an element",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to hover"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["selector"],
            },
        ),
        types.Tool(
            name="select_option",
            description="Select an option in a <select> dropdown by value or label",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of <select>"},
                    "value": {"type": "string", "description": "Option value or label to select"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["selector", "value"],
            },
        ),
        types.Tool(
            name="get_attribute",
            description="Get an attribute value from an element",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "attribute": {"type": "string", "description": "Attribute name (e.g. href, src, class)"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["selector", "attribute"],
            },
        ),
        types.Tool(
            name="save_pdf",
            description="Save the current page as PDF",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to save PDF"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="press_key",
            description="Press a keyboard key (optionally on a specific element)",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key name (e.g. Enter, Tab, ArrowDown, Escape)"},
                    "selector": {"type": "string", "description": "Optional CSS selector to focus first"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                },
                "required": ["key"],
            },
        ),
        types.Tool(
            name="new_page",
            description="Create a new browser page/tab",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Unique ID for the new page"},
                },
                "required": ["page_id"],
            },
        ),
        types.Tool(
            name="switch_page",
            description="Switch to a different browser page/tab",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID to switch to"},
                },
                "required": ["page_id"],
            },
        ),
        types.Tool(
            name="list_pages",
            description="List all open browser pages/tabs",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="wait_for_selector",
            description="Wait for an element to appear on the page",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to wait for"},
                    "page_id": {"type": "string", "description": "Optional page ID"},
                    "timeout": {"type": "number", "description": "Timeout in ms (default 30000)"},
                },
                "required": ["selector"],
            },
        ),
    ]


# ─── Tool handler ───────────────────────────────────────────────────────────

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    global current_page_id

    if not arguments:
        arguments = {}

    await ensure_browser()

    # ── navigate ──
    if name == "navigate":
        url = arguments.get("url")
        if not url:
            raise ValueError("URL is required")
        if not url.startswith("http"):
            url = "https://" + url
        pg = get_active_page(arguments.get("page_id"))
        resp = await pg.goto(url, wait_until="domcontentloaded")
        title = await pg.title()
        return [types.TextContent(
            type="text",
            text=f"Navigated to: {pg.url}\nTitle: {title}\nStatus: {resp.status if resp else 'N/A'}"
        )]

    # ── click ──
    elif name == "click":
        selector = arguments.get("selector")
        if not selector:
            raise ValueError("Selector is required")
        pg = get_active_page(arguments.get("page_id"))
        await pg.click(selector)
        return [types.TextContent(type="text", text=f"Clicked: {selector}")]

    # ── type ──
    elif name == "type":
        selector = arguments.get("selector")
        text = arguments.get("text")
        if not selector or text is None:
            raise ValueError("Selector and text are required")
        pg = get_active_page(arguments.get("page_id"))
        await pg.fill(selector, str(text))
        return [types.TextContent(type="text", text=f"Typed '{text}' into {selector}")]

    # ── get_text ──
    elif name == "get_text":
        selector = arguments.get("selector")
        if not selector:
            raise ValueError("Selector is required")
        pg = get_active_page(arguments.get("page_id"))
        text = await pg.text_content(selector)
        return [types.TextContent(type="text", text=text or "(empty)")]

    # ── get_page_content ──
    elif name == "get_page_content":
        pg = get_active_page(arguments.get("page_id"))
        content = await pg.content()
        return [types.TextContent(type="text", text=content)]

    # ── take_screenshot ──
    elif name == "take_screenshot":
        pg = get_active_page(arguments.get("page_id"))
        selector = arguments.get("selector")
        full_page = arguments.get("full_page", False)

        if selector:
            screenshot = await pg.locator(selector).screenshot()
        else:
            screenshot = await pg.screenshot(full_page=full_page)

        base64_image = base64.b64encode(screenshot).decode("utf-8")
        return [types.ImageContent(
            type="image",
            image=types.ImageData(mime_type="image/png", data=base64_image),
        )]

    # ── evaluate ──
    elif name == "evaluate":
        script = arguments.get("script")
        if not script:
            raise ValueError("Script is required")
        pg = get_active_page(arguments.get("page_id"))
        result = await pg.evaluate(script)
        return [types.TextContent(type="text", text=str(result))]

    # ── scroll ──
    elif name == "scroll":
        x = arguments.get("x", 0)
        y = arguments.get("y", 0)
        pg = get_active_page(arguments.get("page_id"))
        await pg.evaluate(f"window.scrollBy({x}, {y})")
        pos = await pg.evaluate("({x: window.scrollX, y: window.scrollY})")
        return [types.TextContent(type="text", text=f"Scrolled by ({x},{y}), now at {pos}")]

    # ── hover ──
    elif name == "hover":
        selector = arguments.get("selector")
        if not selector:
            raise ValueError("Selector is required")
        pg = get_active_page(arguments.get("page_id"))
        await pg.hover(selector)
        return [types.TextContent(type="text", text=f"Hovered: {selector}")]

    # ── select_option ──
    elif name == "select_option":
        selector = arguments.get("selector")
        value = arguments.get("value")
        if not selector or not value:
            raise ValueError("Selector and value are required")
        pg = get_active_page(arguments.get("page_id"))
        await pg.select_option(selector, value)
        return [types.TextContent(type="text", text=f"Selected '{value}' in {selector}")]

    # ── get_attribute ──
    elif name == "get_attribute":
        selector = arguments.get("selector")
        attr = arguments.get("attribute")
        if not selector or not attr:
            raise ValueError("Selector and attribute are required")
        pg = get_active_page(arguments.get("page_id"))
        value = await pg.get_attribute(selector, attr)
        return [types.TextContent(type="text", text=value or "(null)")]

    # ── save_pdf ──
    elif name == "save_pdf":
        path = arguments.get("path")
        if not path:
            raise ValueError("Path is required")
        pg = get_active_page(arguments.get("page_id"))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await pg.pdf(path=path)
        return [types.TextContent(type="text", text=f"PDF saved to: {path}")]

    # ── press_key ──
    elif name == "press_key":
        key = arguments.get("key")
        if not key:
            raise ValueError("Key is required")
        pg = get_active_page(arguments.get("page_id"))
        selector = arguments.get("selector")
        if selector:
            await pg.press(selector, key)
        else:
            await pg.keyboard.press(key)
        return [types.TextContent(type="text", text=f"Pressed: {key}")]

    # ── new_page ──
    elif name == "new_page":
        page_id = arguments.get("page_id")
        if not page_id:
            raise ValueError("Page ID is required")
        if page_id in pages:
            raise ValueError(f"Page ID '{page_id}' already exists")
        new_pg = await context.new_page()
        pages[page_id] = new_pg
        current_page_id = page_id
        return [types.TextContent(type="text", text=f"Created new page: {page_id}")]

    # ── switch_page ──
    elif name == "switch_page":
        page_id = arguments.get("page_id")
        if not page_id:
            raise ValueError("Page ID is required")
        if page_id not in pages:
            raise ValueError(f"Page ID '{page_id}' not found")
        current_page_id = page_id
        return [types.TextContent(type="text", text=f"Switched to page: {page_id}")]

    # ── list_pages ──
    elif name == "list_pages":
        info = [f"{pid}: {p.url} {'(active)' if pid == current_page_id else ''}" for pid, p in pages.items()]
        return [types.TextContent(type="text", text="Open pages:\n" + "\n".join(info))]

    # ── wait_for_selector ──
    elif name == "wait_for_selector":
        selector = arguments.get("selector")
        if not selector:
            raise ValueError("Selector is required")
        timeout = arguments.get("timeout", 30000)
        pg = get_active_page(arguments.get("page_id"))
        try:
            await pg.wait_for_selector(selector, timeout=timeout)
            return [types.TextContent(type="text", text=f"Element found: {selector}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Timeout waiting for: {selector} ({e})")]

    else:
        raise ValueError(f"Unknown tool: {name}")

    # Notify clients that resources changed
    await server.request_context.session.send_resource_list_changed()


# ─── Prompts ────────────────────────────────────────────────────────────────

@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="analyze-page",
            description="Analyze the current page — structure, forms, content",
            arguments=[
                types.PromptArgument(name="page_id", description="Page ID", required=False),
                types.PromptArgument(
                    name="focus",
                    description="What to focus on: full, forms, navigation, text",
                    required=False,
                ),
            ],
        )
    ]


@server.get_prompt()
async def handle_get_prompt(
    name: str, arguments: dict[str, str] | None
) -> types.GetPromptResult:
    global current_page_id
    arguments = arguments or {}
    page_id = arguments.get("page_id", current_page_id)
    focus = arguments.get("focus", "full")

    if page_id not in pages:
        raise ValueError(f"Page ID '{page_id}' not found")

    pg = pages[page_id]
    url = pg.url
    title = await pg.title()
    screenshot = await pg.screenshot()
    screenshot_b64 = base64.b64encode(screenshot).decode("utf-8")

    prompt_text = f"Analyze this web page at URL: {url}\nTitle: {title}\n\n"

    if focus == "forms":
        prompt_text += "Focus on identifying all input forms, their fields, and how to interact with them.\n\n"
        form_info = await pg.evaluate("""() => {
            const forms = Array.from(document.querySelectorAll('form'));
            return forms.map(form => ({
                id: form.id || '(no id)',
                action: form.action,
                method: form.method,
                inputs: Array.from(form.querySelectorAll('input, select, textarea, button')).map(input => ({
                    tag: input.tagName.toLowerCase() === 'input' ? input.type : input.tagName.toLowerCase(),
                    name: input.name || '',
                    id: input.id || '',
                    placeholder: input.placeholder || '',
                    required: input.required || false
                }))
            }));
        }""")
        prompt_text += f"Form information: {form_info}\n\n"
    elif focus == "navigation":
        prompt_text += "Focus on navigation elements, links, and site structure.\n\n"
        nav_info = await pg.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 30);
            return links.map(a => ({text: a.textContent.trim().substring(0, 80), href: a.href}));
        }""")
        prompt_text += f"Links: {nav_info}\n\n"
    elif focus == "text":
        prompt_text += "Focus on extracting and summarizing the main text content.\n\n"
        text_content = await pg.evaluate("""() => {
            const main = document.querySelector('main, article, [role="main"]');
            return main ? main.textContent.trim().substring(0, 5000) : document.body.textContent.trim().substring(0, 5000);
        }""")
        prompt_text += f"Main content:\n{text_content}\n\n"
    else:
        prompt_text += "Provide a complete analysis of page elements, content, and functionality.\n"

    return types.GetPromptResult(
        description=f"Analyze page at {url}",
        messages=[
            types.PromptMessage(
                role="user",
                content=[
                    types.TextContent(type="text", text=prompt_text),
                    types.ImageContent(
                        type="image",
                        image=types.ImageData(mime_type="image/png", data=screenshot_b64),
                    ),
                ],
            )
        ],
    )


# ─── Main ───────────────────────────────────────────────────────────────────

async def main():
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="browser-mcp",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await cleanup()


if __name__ == "__main__":
    asyncio.run(main())
