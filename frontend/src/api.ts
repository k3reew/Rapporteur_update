export async function request(path: string, init?: RequestInit) {
  const response = await fetch(path, init);
  if (!response.ok) {
    const error: { message?: string; request_id?: string } | null = await response.json().catch(() => null);
    const id = error?.request_id ?? response.headers.get('X-Request-ID');
    throw new Error((error?.message ?? `Ошибка HTTP ${response.status}`) + (id ? ` · Запрос ${id}` : ''));
  }
  return response;
}

export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await request(path, { signal })).json();
}
