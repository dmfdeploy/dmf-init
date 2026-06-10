/** Map raw browser fetch failures ("Load failed" / "Failed to fetch") to
 * operator-actionable content. A network-level TypeError means the init
 * container itself became unreachable mid-request — the operator needs to
 * look at the container, not puzzle over a two-word browser message. */
export function describeFetchError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  if (
    error instanceof TypeError ||
    /load failed|failed to fetch|networkerror/i.test(message)
  ) {
    return (
      'Lost connection to the init container — it may have stopped or been ' +
      'killed (e.g. out of memory). Check the terminal where you started ' +
      'docker run; if the container exited, start it again and open the ' +
      'fresh launch link it prints.'
    )
  }
  return message
}
