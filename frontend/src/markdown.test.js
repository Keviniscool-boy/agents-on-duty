// 安全 Markdown 渲染回归：先转义 HTML，再白名单渲染，杜绝 XSS。
import test from "node:test";
import assert from "node:assert/strict";

import { escapeHtml, renderSafeMarkdown } from "./markdown.js";

test("raw HTML is escaped, never rendered", () => {
  const out = renderSafeMarkdown("<img src=x onerror=alert(1)>");
  assert.equal(out.includes("<img"), false);
  assert.ok(out.includes("&lt;img"));
});

test("script tags are escaped", () => {
  const out = renderSafeMarkdown("<script>alert('xss')</script>");
  assert.equal(out.includes("<script"), false);
  assert.ok(out.includes("&lt;script&gt;"));
  assert.ok(out.includes("&#39;"), "引号需要转义");
});

test("event handlers and javascript: urls stay inert text", () => {
  const out = renderSafeMarkdown(
    '点击 [链接](javascript:alert(1)) 和 <a href="javascript:alert(1)">x</a>',
  );
  assert.equal(out.includes("<a "), false);
  assert.ok(out.includes("&lt;a href="));
});

test("bold renders as strong", () => {
  assert.equal(renderSafeMarkdown("**重要**"), "<strong>重要</strong>");
});

test("inline code renders as code and blocks bold inside", () => {
  assert.equal(renderSafeMarkdown("`**x**`"), "<code>**x**</code>");
});

test("bullet lists render as ul/li", () => {
  const out = renderSafeMarkdown("说明：\n- 第一条\n- 第二条");
  assert.ok(out.includes("<ul>"));
  assert.ok(out.includes("<li>第一条</li>"));
  assert.ok(out.includes("<li>第二条</li>"));
});

test("newlines are preserved for pre-wrap bubbles", () => {
  const out = renderSafeMarkdown("第一行\n第二行");
  assert.ok(out.includes("\n"));
  assert.ok(out.includes("第一行"));
  assert.ok(out.includes("第二行"));
});

test("escapeHtml covers the five dangerous characters", () => {
  assert.equal(escapeHtml(`<>&"'`), "&lt;&gt;&amp;&quot;&#39;");
});

test("quote-heavy content cannot break out of attribute context", () => {
  const out = renderSafeMarkdown('" onmouseover="alert(1)');
  assert.equal(out.includes('"'), false);
  assert.ok(out.includes("&quot;"));
});
