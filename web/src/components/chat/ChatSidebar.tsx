import * as React from 'react'
import { Bot, Send, User, ChevronRight, ChevronLeft, Loader2, Play } from 'lucide-react'
import { cn } from '@/lib/utils'

interface ChatSidebarProps {
  knowledgeBaseId: string;
}

export function ChatSidebar({ knowledgeBaseId }: ChatSidebarProps) {
  const [isOpen, setIsOpen] = React.useState(false)
  const [messages, setMessages] = React.useState<any[]>([])
  const [input, setInput] = React.useState('')
  const [isGenerating, setIsGenerating] = React.useState(false)
  const [isModelReady, setIsModelReady] = React.useState(false)
  const [status, setStatus] = React.useState('Loading model...')

  const workerRef = React.useRef<Worker | null>(null)
  const messagesEndRef = React.useRef<HTMLDivElement>(null)

  React.useEffect(() => {
    // Initialize worker
    workerRef.current = new Worker(new URL('../../workers/ai.worker.ts', import.meta.url))

    workerRef.current.onmessage = (e) => {
      const { type, message, text, info } = e.data

      if (type === 'status') {
        setStatus(message)
        if (message === 'Ready') {
          setIsModelReady(true)
        }
      } else if (type === 'progress') {
        if (info.status === 'progress_total') {
          setStatus(`Downloading... ${Math.round(info.progress)}%`)
        }
      } else if (type === 'start_generation') {
        setIsGenerating(true)
        setMessages((prev) => [...prev, { role: 'assistant', content: '' }])
      } else if (type === 'chunk') {
        setMessages((prev) => {
          const newMessages = [...prev]
          const lastIndex = newMessages.length - 1
          if (newMessages[lastIndex].role === 'assistant') {
            newMessages[lastIndex].content += text
          }
          return newMessages
        })
      } else if (type === 'complete') {
        const toolCallMatch = text.match(/<tool_call>([\s\S]*?)<\/tool_call>/)
        if (toolCallMatch) {
          setIsGenerating(true)
          setStatus('Executing tool...')
          try {
            const toolReq = JSON.parse(toolCallMatch[1].trim())
            handleToolCall(toolReq, text)
          } catch (err) {
            console.error('Failed to parse tool call', err)
            setIsGenerating(false)
            setStatus('Ready')
          }
        } else {
          setIsGenerating(false)
          setStatus('Ready')
        }
      } else if (type === 'error') {
        console.error('AI Worker error:', message)
        setIsGenerating(false)
        setStatus('Error loading model')
      }
    }

    return () => {
      workerRef.current?.terminate()
    }
  }, [])

  const handleToolCall = async (toolReq: any, assistantMessage: string) => {
    const { tool_name, arguments: args } = toolReq
    let result = ''

    // Add assistant's tool call message
    const msgsWithToolCall = [...messages, { role: 'assistant', content: assistantMessage }]
    setMessages(msgsWithToolCall)

    try {
      const { useUserStore } = await import('@/stores/useUserStore')
      const token = useUserStore.getState().accessToken
      if (!token) throw new Error('Not authenticated')

      const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

      const res = await fetch(`${API_URL}/api/tools/${tool_name}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          knowledge_base: knowledgeBaseId,
          ...args
        })
      })

      if (!res.ok) {
        result = `Tool execution failed: ${await res.text()}`
      } else {
        const data = await res.json()
        result = typeof data === 'string' ? data : JSON.stringify(data)
      }
    } catch (err: any) {
      result = `Tool execution error: ${err.message}`
    }

    // Pass the tool result back to the model
    const newMessages = [...msgsWithToolCall, { role: 'user', content: `<tool_response>\n${result}\n</tool_response>` }]
    setMessages(newMessages)
    runAgentLoop(newMessages)
  }

  const loadModel = () => {
    workerRef.current?.postMessage({ type: 'load' })
  }

  React.useEffect(() => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  const handleSend = async (textToUse?: string) => {
    const text = typeof textToUse === 'string' ? textToUse : input.trim()
    if (!text || !isModelReady || isGenerating) return

    const newMessages = [...messages, { role: 'user', content: text }]
    setMessages(newMessages)
    setInput('')

    runAgentLoop(newMessages)
  }

  const runAgentLoop = async (currentMessages: any[]) => {
    // Convert to simple format for Gemma
    const formattedMessages = currentMessages.map(m => ({
      role: m.role,
      content: m.content
    }))

    // Provide system prompt to explain its role as LLM Wiki agent and explicitly define available tools using a ReAct-like pattern
    const toolInstructions = `You are Gemma, an AI assistant built into the LLM Wiki. Your job is to answer the user's questions and manage their wiki.

You have access to the following tools:
1. "search": Search for documents. Provide a JSON object with {"mode": "search" | "list", "query": "..."}
2. "read": Read a document. Provide a JSON object with {"path": "..."}
3. "write": Create or edit a document. Provide a JSON object with {"command": "create" | "append" | "str_replace", "path": "...", "title": "...", "content": "..."}
4. "delete": Delete a document. Provide a JSON object with {"path": "..."}

To use a tool, you MUST output EXACTLY this format:
<tool_call>
{"tool_name": "search", "arguments": {"mode": "search", "query": "my topic"}}
</tool_call>

Wait for the user to provide the tool response before continuing your thought process.
Do NOT use tools unless necessary to fulfill the user's request.`

    formattedMessages.unshift({
      role: 'system',
      content: toolInstructions
    })

    workerRef.current?.postMessage({
      type: 'generate',
      messages: formattedMessages
    })
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend(input.trim())
    }
  }

  if (!isOpen) {
    return (
      <div
        className="fixed right-0 top-1/2 -translate-y-1/2 bg-background border border-border border-r-0 rounded-l-xl p-2 cursor-pointer shadow-sm hover:bg-accent transition-colors z-50"
        onClick={() => setIsOpen(true)}
      >
        <Bot className="size-5 text-primary mb-1" />
        <ChevronLeft className="size-4 text-muted-foreground mx-auto" />
      </div>
    )
  }

  return (
    <div className={cn(
      "w-80 border-l border-border bg-background flex flex-col h-full absolute right-0 top-0 bottom-0 z-40 transition-transform duration-300",
      isOpen ? "translate-x-0" : "translate-x-full"
    )}>
      <div className="h-14 border-b border-border flex items-center px-4 justify-between shrink-0">
        <div className="flex items-center gap-2">
          <Bot className="size-5 text-primary" />
          <span className="font-medium text-sm">Gemma Assistant</span>
        </div>
        <button
          onClick={() => setIsOpen(false)}
          className="p-1 hover:bg-accent rounded-md text-muted-foreground transition-colors"
        >
          <ChevronRight className="size-4" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4 text-sm flex flex-col">
        {!isModelReady ? (
          <div className="flex flex-col items-center justify-center flex-1 text-center text-muted-foreground gap-4">
            <Bot className="size-8 opacity-20" />
            <p className="text-sm px-4">
              Local AI models run entirely in your browser using WebGPU.
              <br/><br/>
              The Gemma 4 E4B model is ~2GB.
            </p>
            <button
              onClick={loadModel}
              className="inline-flex items-center gap-2 rounded-full bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:opacity-90"
            >
              <Play className="size-3.5" />
              Download & Load Model
            </button>
            <p className="text-xs">{status}</p>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center flex-1 text-center text-muted-foreground">
            <Bot className="size-8 opacity-20 mb-3" />
            <p>I'm ready! How can I help you manage your wiki?</p>
          </div>
        ) : (
          messages.map((msg, i) => (
            <div
              key={i}
              className={cn(
                "flex gap-3",
                msg.role === 'user' ? "flex-row-reverse" : ""
              )}
            >
              <div className="shrink-0 pt-0.5">
                {msg.role === 'user' ? (
                  <div className="size-6 rounded-full bg-accent flex items-center justify-center border border-border">
                    <User className="size-3.5" />
                  </div>
                ) : (
                  <div className="size-6 rounded-full bg-primary/10 flex items-center justify-center border border-primary/20 text-primary">
                    <Bot className="size-3.5" />
                  </div>
                )}
              </div>
              <div className={cn(
                "rounded-xl px-3 py-2 max-w-[85%]",
                msg.role === 'user'
                  ? "bg-foreground text-background"
                  : "bg-accent border border-border whitespace-pre-wrap break-words"
              )}>
                {msg.content || (isGenerating && i === messages.length - 1 ? <Loader2 className="size-3 animate-spin opacity-50" /> : null)}
              </div>
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="p-3 border-t border-border shrink-0 bg-background/50 backdrop-blur-sm">
        <div className="relative">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={isModelReady ? "Ask Gemma..." : "Waiting for model..."}
            disabled={!isModelReady || isGenerating}
            rows={1}
            className="w-full resize-none rounded-xl border border-input bg-transparent px-3 py-2.5 pr-10 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px] max-h-32"
          />
          <button
            onClick={() => handleSend(input.trim())}
            disabled={!input.trim() || !isModelReady || isGenerating}
            className="absolute right-2 bottom-2 p-1.5 rounded-lg bg-primary text-primary-foreground disabled:opacity-50 transition-opacity"
          >
            <Send className="size-3.5" />
          </button>
        </div>
      </div>
    </div>
  )
}