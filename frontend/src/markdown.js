// 聊天气泡的安全 Markdown 渲染：先整体转义 HTML，再按白名单渲染
// 行内 code、粗体、无序列表。不支持链接、标题、图片——URL 一律以
// 纯文本显示，杜绝原始 HTML 与不安全 URL 注入。
// 换行交给 .bubble 的 white-space: pre-wrap 处理，这里不生成 <br>。
// 注意：.bubble 是 pre-wrap，<ul> 内部与两侧不能有多余换行文本节点，
// 否则会渲染成列表项之间的空行；块级元素必须"零内部换行"整体输出。

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
  const blocks = [];
  let listItems = [];

  const flushList = () => {
    if (listItems.length) {
      blocks.push(`<ul>${listItems.join("")}</ul>`);
      listItems = [];
    }
  };

  for (const line of lines) {
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    if (bullet) {
      listItems.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    flushList();
    blocks.push(renderInline(line));
  }
  flushList();
  return blocks.join("\n");
}

// 会话标题等纯文本位：去掉 Markdown 标记，避免 "**7×24小时**" 这类原文。
export function plainText(text) {
  return String(text ?? "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^\s*[-*•]\s+/gm, "");
}
