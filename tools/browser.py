#!/usr/bin/env python3
"""
Browser automation CLI for Claude Code.
Wraps Playwright sync API — headless Chromium by default.

Usage:
  python tools/browser.py navigate <url>              # 打开页面
  python tools/browser.py screenshot [url]            # 截图
  python tools/browser.py click <selector>            # 点击元素
  python tools/browser.py fill <selector> <text>      # 填充输入框
  python tools/browser.py extract <selector>          # 提取文本
  python tools/browser.py eval <javascript>           # 执行 JS
  python tools/browser.py content                     # 获取完整 HTML
  python tools/browser.py pdf [url]                   # 保存 PDF
  python tools/browser.py wait <selector|ms>          # 等待元素/时间
  python tools/browser.py scroll [x] [y]              # 滚动页面
  python tools/browser.py title                       # 获取页面标题
  python tools/browser.py select <selector> <value>   # 下拉选择
  python tools/browser.py hover <selector>            # 悬停元素
  python tools/browser.py attr <selector> <name>      # 获取属性值
  python tools/browser.py submit <selector>           # 提交表单
  python tools/browser.py press <key>                 # 按键

Options:
  --url/-u      目标 URL
  --headed      显示浏览器窗口 (默认无头)
  --browser     浏览器: chromium (默认) | firefox | webkit
  --output/-o   输出文件路径
  --selector/-s CSS 选择器
  --timeout/-t  超时毫秒 (默认 30000)
  --json/-j     JSON 格式输出
  --wait-for    操作前等待某元素出现
  --viewport    视口大小 WxH (默认 1280x800)
  --user-agent  自定义 User-Agent
  --cookie      Cookie 字符串 (name=value)
  --session     使用持久会话 (保持登录)
  --slow-mo     操作间隔延迟 ms
  --block-media 屏蔽图片/视频加载 (更快)
"""

import argparse
import json
import sys
import os
import time
from pathlib import Path

PYTHON = r"C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe"
DATA_DIR = Path(__file__).parent.parent / "data" / "browser"
SESSION_DIR = DATA_DIR / "sessions"


def get_playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright().start()


def create_browser(playwright, args):
    """Launch browser with configured options."""
    browser_type = args.browser or "chromium"
    launch_options = {"headless": not args.headed}

    if args.slow_mo:
        launch_options["slow_mo"] = args.slow_mo

    if browser_type == "chromium":
        browser = playwright.chromium.launch(**launch_options)
    elif browser_type == "firefox":
        browser = playwright.firefox.launch(**launch_options)
    elif browser_type == "webkit":
        browser = playwright.webkit.launch(**launch_options)
    else:
        raise ValueError(f"Unknown browser: {browser_type}")

    return browser


def create_context(browser, args):
    """Create browser context with configured options."""
    context_options = {}

    if args.viewport:
        w, h = args.viewport.split("x")
        context_options["viewport"] = {"width": int(w), "height": int(h)}
    else:
        context_options["viewport"] = {"width": 1280, "height": 800}

    if args.user_agent:
        context_options["user_agent"] = args.user_agent

    if args.block_media:
        # Block images, fonts, and media for faster loading
        context_options["bypass_csp"] = True

    # Persistent session directory
    if args.session:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session_path = SESSION_DIR / args.session
        context = browser.launch_persistent_context(
            user_data_dir=str(session_path),
            headless=not args.headed,
            **{k: v for k, v in context_options.items() if k != "bypass_csp"},
        )
    else:
        context = browser.new_context(**context_options)

    # Set cookies if provided
    if args.cookie:
        cookies = []
        for c in args.cookie.split(";"):
            name, value = c.strip().split("=", 1)
            cookies.append({"name": name.strip(), "value": value.strip(), "url": args.url or "about:blank"})
        context.add_cookies(cookies)

    # Route interception to block media
    if args.block_media:

        def block_media_route(route):
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                route.abort()
            else:
                route.continue_()

        context.route("**/*", block_media_route)

    return context


def get_page(context, args):
    """Get or create a page, optionally navigate to URL."""
    if context.pages:
        page = context.pages[0]
    else:
        page = context.new_page()

    url = args.url
    if url:
        if not url.startswith("http"):
            url = "https://" + url
        page.goto(url, timeout=args.timeout or 30000)

    return page


def wait_before_action(page, args):
    """Wait for selector if --wait-for is specified."""
    if args.wait_for:
        try:
            page.wait_for_selector(args.wait_for, timeout=args.timeout or 30000)
        except Exception as e:
            print_json_error(f"Timeout waiting for '{args.wait_for}': {e}", args)
            return False
    return True


def print_json(data, args):
    """Output as JSON or plain text."""
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict):
            for k, v in data.items():
                print(f"{k}: {v}")
        elif isinstance(data, list):
            for item in data:
                print(item)
        else:
            print(data)


def print_json_error(msg, args):
    """Output error as JSON or plain text."""
    if args.json:
        print(json.dumps({"error": str(msg)}, ensure_ascii=False))
    else:
        print(f"ERROR: {msg}", file=sys.stderr)


# ─── Actions ────────────────────────────────────────────────────────────────

def cmd_navigate(page, args):
    url = args.url
    if not url:
        print_json_error("URL required. Use --url <url>", args)
        return 1
    if not url.startswith("http"):
        url = "https://" + url
    resp = page.goto(url, timeout=args.timeout or 30000, wait_until="domcontentloaded")
    result = {
        "url": page.url,
        "title": page.title(),
        "status": resp.status if resp else None,
    }
    print_json(result, args)
    return 0


def cmd_screenshot(page, args):
    if args.url:
        if not args.url.startswith("http"):
            args.url = "https://" + args.url
        page.goto(args.url, timeout=args.timeout or 30000, wait_until="domcontentloaded")

    wait_before_action(page, args)

    screenshot_options = {"full_page": True}
    if args.selector:
        el = page.locator(args.selector).first
        screenshot_bytes = el.screenshot()
    else:
        screenshot_bytes = page.screenshot(**screenshot_options)

    output_path = args.output
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(screenshot_bytes)
        result = {"screenshot_saved": output_path, "url": page.url, "title": page.title()}
    else:
        # Default: save to data/browser/screenshots/
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"screenshot_{ts}.png"
        fpath = DATA_DIR / fname
        fpath.write_bytes(screenshot_bytes)
        result = {"screenshot_saved": str(fpath), "url": page.url, "title": page.title()}

    print_json(result, args)
    return 0


def cmd_click(page, args):
    if not args.selector:
        print_json_error("Selector required. Use --selector <css>", args)
        return 1

    wait_before_action(page, args)
    page.click(args.selector, timeout=args.timeout or 30000)

    result = {"clicked": args.selector, "url": page.url}
    print_json(result, args)
    return 0


def cmd_fill(page, args):
    if not args.selector:
        print_json_error("Selector required. Use --selector <css>", args)
        return 1

    wait_before_action(page, args)

    # text is the first positional after selector, or from remaining args
    if args.text:
        text = args.text
    else:
        print_json_error("Text required: fill <selector> <text>", args)
        return 1

    page.fill(args.selector, text, timeout=args.timeout or 30000)

    result = {"filled": args.selector, "text": text, "url": page.url}
    print_json(result, args)
    return 0


def cmd_extract(page, args):
    if not args.selector:
        print_json_error("Selector required. Use --selector <css>", args)
        return 1

    wait_before_action(page, args)

    elements = page.locator(args.selector).all()
    texts = []
    for el in elements:
        try:
            texts.append(el.text_content().strip())
        except Exception:
            pass

    if args.json:
        print(json.dumps({"selector": args.selector, "count": len(texts), "texts": texts}, ensure_ascii=False, indent=2))
    else:
        for t in texts:
            print(t)
    return 0


def cmd_eval(page, args):
    js_code = args.js_code
    if not js_code:
        print_json_error("JavaScript code required", args)
        return 1

    wait_before_action(page, args)

    try:
        result = page.evaluate(js_code)
        if args.json:
            print(json.dumps({"result": result}, ensure_ascii=False, indent=2, default=str))
        else:
            print(result)
    except Exception as e:
        print_json_error(f"eval error: {e}", args)
        return 1
    return 0


def cmd_content(page, args):
    wait_before_action(page, args)
    html = page.content()
    if args.output:
        Path(args.output).write_text(html, encoding="utf-8")
        result = {"html_saved": args.output, "length": len(html), "url": page.url}
        print_json(result, args)
    else:
        if args.json:
            print(json.dumps({"url": page.url, "length": len(html)}, ensure_ascii=False))
        else:
            print(html)
    return 0


def cmd_pdf(page, args):
    if args.url:
        if not args.url.startswith("http"):
            args.url = "https://" + args.url
        page.goto(args.url, timeout=args.timeout or 30000, wait_until="domcontentloaded")

    wait_before_action(page, args)

    output_path = args.output
    if not output_path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        output_path = str(DATA_DIR / f"page_{ts}.pdf")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    page.pdf(path=output_path)

    result = {"pdf_saved": output_path, "url": page.url, "title": page.title()}
    print_json(result, args)
    return 0


def cmd_wait(page, args):
    target = args.wait_target
    if not target:
        print_json_error("Wait target required: wait <selector|ms>", args)
        return 1

    # Check if it's a number (milliseconds)
    try:
        ms = int(target)
        page.wait_for_timeout(ms)
        result = {"waited_ms": ms}
        print_json(result, args)
        return 0
    except ValueError:
        pass

    # Otherwise treat as selector
    try:
        page.wait_for_selector(target, timeout=args.timeout or 30000)
        result = {"found": target}
        print_json(result, args)
    except Exception as e:
        print_json_error(f"Timeout waiting for '{target}': {e}", args)
        return 1
    return 0


def cmd_scroll(page, args):
    x = args.scroll_x or 0
    y = args.scroll_y or 0
    page.evaluate(f"window.scrollBy({x}, {y})")
    scroll_pos = page.evaluate("({x: window.scrollX, y: window.scrollY})")
    result = {"scrolled_by": {"x": x, "y": y}, "position": scroll_pos}
    print_json(result, args)
    return 0


def cmd_title(page, args):
    title = page.title()
    if args.json:
        print(json.dumps({"title": title, "url": page.url}, ensure_ascii=False))
    else:
        print(title)
    return 0


def cmd_select(page, args):
    """Select an option in a <select> element."""
    if not args.selector:
        print_json_error("Selector required for select", args)
        return 1
    value = args.text  # reuse text positional for value
    if not value:
        print_json_error("Value required: select <selector> <value>", args)
        return 1

    wait_before_action(page, args)
    page.select_option(args.selector, value, timeout=args.timeout or 30000)

    result = {"selected": args.selector, "value": value, "url": page.url}
    print_json(result, args)
    return 0


def cmd_hover(page, args):
    if not args.selector:
        print_json_error("Selector required for hover", args)
        return 1

    wait_before_action(page, args)
    page.hover(args.selector, timeout=args.timeout or 30000)

    result = {"hovered": args.selector, "url": page.url}
    print_json(result, args)
    return 0


def cmd_attr(page, args):
    """Get an attribute value from an element."""
    if not args.selector:
        print_json_error("Selector required", args)
        return 1
    attr_name = args.text  # reuse text positional for attribute name
    if not attr_name:
        print_json_error("Attribute name required: attr <selector> <name>", args)
        return 1

    wait_before_action(page, args)
    value = page.get_attribute(args.selector, attr_name)

    if args.json:
        print(json.dumps({"selector": args.selector, "attribute": attr_name, "value": value}, ensure_ascii=False))
    else:
        print(value or "")
    return 0


def cmd_submit(page, args):
    """Submit a form by pressing Enter in the given selector."""
    if not args.selector:
        print_json_error("Selector required for submit", args)
        return 1

    wait_before_action(page, args)
    page.press(args.selector, "Enter")

    result = {"submitted": args.selector, "url": page.url}
    print_json(result, args)
    return 0


def cmd_press(page, args):
    """Press a keyboard key."""
    key = args.key_name
    if not key:
        print_json_error("Key name required: press <key>", args)
        return 1

    if args.selector:
        page.press(args.selector, key)
    else:
        page.keyboard.press(key)

    result = {"pressed": key, "selector": args.selector, "url": page.url}
    print_json(result, args)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Browser automation CLI for Claude Code (Playwright)",
        usage="python tools/browser.py <action> [options]",
    )

    parser.add_argument("action", nargs="?", help="Action to perform")
    parser.add_argument("extra", nargs="*", help="Extra positional args (selector, text, etc.)")

    parser.add_argument("--url", "-u", help="Target URL")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--selector", "-s", help="CSS selector")
    parser.add_argument("--timeout", "-t", type=int, default=30000, help="Timeout in ms")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--wait-for", help="Wait for selector before action")
    parser.add_argument("--viewport", help="Viewport WxH (e.g. 1920x1080)")
    parser.add_argument("--user-agent", help="Custom User-Agent")
    parser.add_argument("--cookie", help="Cookie string (name=value)")
    parser.add_argument("--session", help="Persistent session name")
    parser.add_argument("--slow-mo", type=int, help="Slow motion delay (ms)")
    parser.add_argument("--block-media", action="store_true", help="Block images/media for speed")

    args = parser.parse_args()

    if not args.action:
        parser.print_help()
        return 1

    # Route extra positional args based on action
    action = args.action.lower()

    # Map extra args for different actions
    if action in ("fill", "type"):
        if args.extra:
            if not args.selector:
                args.selector = args.extra[0]
                args.text = args.extra[1] if len(args.extra) > 1 else None
            else:
                args.text = args.extra[0]
        else:
            args.text = None
    elif action == "extract":
        if args.extra and not args.selector:
            args.selector = args.extra[0]
    elif action == "eval":
        if args.extra:
            args.js_code = " ".join(args.extra)
        else:
            args.js_code = None
    elif action == "click":
        if args.extra and not args.selector:
            args.selector = args.extra[0]
    elif action == "wait":
        args.wait_target = args.extra[0] if args.extra else None
    elif action == "scroll":
        args.scroll_x = int(args.extra[0]) if len(args.extra) > 0 else 0
        args.scroll_y = int(args.extra[1]) if len(args.extra) > 1 else None
        if args.scroll_y is None:
            # If only one number given, it's Y (vertical scroll)
            args.scroll_y = args.scroll_x
            args.scroll_x = 0
    elif action == "select":
        if args.extra:
            if not args.selector:
                args.selector = args.extra[0]
                args.text = args.extra[1] if len(args.extra) > 1 else None
            else:
                args.text = args.extra[0]
        else:
            args.text = None
    elif action == "hover":
        if args.extra and not args.selector:
            args.selector = args.extra[0]
    elif action == "attr":
        if args.extra:
            if not args.selector:
                args.selector = args.extra[0]
                args.text = args.extra[1] if len(args.extra) > 1 else None
            else:
                args.text = args.extra[0]
        else:
            args.text = None
    elif action == "submit":
        if args.extra and not args.selector:
            args.selector = args.extra[0]
    elif action == "press":
        if args.extra:
            args.key_name = args.extra[0]
        else:
            args.key_name = None
    elif action == "navigate":
        if args.extra and not args.url:
            args.url = args.extra[0]

    # Launch browser
    pw = get_playwright()
    browser = create_browser(pw, args)
    context = create_context(browser, args)
    page = get_page(context, args)

    # Dispatch
    actions = {
        "navigate": cmd_navigate,
        "screenshot": cmd_screenshot,
        "click": cmd_click,
        "fill": cmd_fill,
        "type": cmd_fill,
        "extract": cmd_extract,
        "eval": cmd_eval,
        "content": cmd_content,
        "pdf": cmd_pdf,
        "wait": cmd_wait,
        "scroll": cmd_scroll,
        "title": cmd_title,
        "select": cmd_select,
        "hover": cmd_hover,
        "attr": cmd_attr,
        "submit": cmd_submit,
        "press": cmd_press,
    }

    handler = actions.get(action)
    if not handler:
        print_json_error(f"Unknown action: {action}. Available: {', '.join(actions.keys())}", args)
        return 1

    try:
        rc = handler(page, args) or 0
    except Exception as e:
        print_json_error(f"{action} failed: {e}", args)
        rc = 1
    finally:
        context.close()
        browser.close()
        pw.stop()

    return rc


if __name__ == "__main__":
    sys.exit(main())
