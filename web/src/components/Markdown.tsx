import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { memo, useMemo } from 'react'

marked.setOptions({ gfm: true, breaks: true })

DOMPurify.addHook('afterSanitizeAttributes', node => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noreferrer')
  }
})

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false }) as string), [text])
  return <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />
})
