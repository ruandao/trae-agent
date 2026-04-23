const clients = new Set();

export function broadcast(obj) {
  const line = `data: ${JSON.stringify(obj)}\n\n`;
  for (const res of clients) {
    try {
      res.write(line);
    } catch {
      clients.delete(res);
    }
  }
}

export function addSseClient(res) {
  clients.add(res);
  res.on('close', () => clients.delete(res));
}

export function ssePingLoop() {
  setInterval(() => {
    for (const res of clients) {
      try {
        res.write(': ping\n\n');
      } catch {
        clients.delete(res);
      }
    }
  }, 30000).unref?.();
}
