// 聊天气泡的安全 Markdown 渲染：先整体转义 HTML，再按白名单渲染
// 行内 code、粗体、无序列表。不支持链接、标题、图片——URL 一律以
// 纯文本显示，杜绝原始 HTML 与不安全 URL 注入。
// 换行交给 .bubble 的 white-space: pre-wrap 处理，这里不生成 <br>。

export function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderInline(line) {
  // 行内 code 优先切分：code 内容不参与粗体等其他替换。
  return line
    .split(/(`[^`]+`)/g)
    .map((part) => {
      if (part.length >= 2 && part.startsWith("`") && part.endsWith("`")) {
        return `<code>${part.slice(1, -1)}</code>`;
      }
      return part.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    })
    .join("");
}

export function renderSafeMarkdown(text) {
  const escaped = escapeHtml(text);
  const lines = escaped.split("\n");
  const out = [];
  let inList = false;

  for (const line of lines) {
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    if (bullet) {
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
    out.push(renderInline(line));
  }
  if (inList) out.push("</ul>");
  return out.join("\n");
}
