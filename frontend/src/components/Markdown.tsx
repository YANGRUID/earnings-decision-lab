import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Renders real Markdown (headings, bold/italic, lists, tables, inline
 * code) as actual elements instead of showing raw `**`/`##` tokens.
 * react-markdown never executes embedded HTML by default (no rehype-raw
 * plugin is used here) -- LLM-generated content is rendered as safe React
 * elements only, never via dangerouslySetInnerHTML. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
