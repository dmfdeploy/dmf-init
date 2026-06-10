export async function readNdjson(
  response: Response,
  onEvent: (ev: Record<string, unknown>) => void,
): Promise<void> {
  const body = response.body
  if (!body) {
    throw new Error('response did not stream')
  }

  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''

    for (const rawLine of lines) {
      if (!rawLine.trim()) continue
      onEvent(JSON.parse(rawLine) as Record<string, unknown>)
    }
  }

  if (buffer.trim()) {
    onEvent(JSON.parse(buffer) as Record<string, unknown>)
  }
}
