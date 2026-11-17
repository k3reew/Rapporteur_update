export async function request(path: string, init?: RequestInit) {
  const response = await fetch(path, init);
  if (!response.ok) {
    const error: { message?: string } | null = await response.json().catch(() => null);
    throw new Error(error?.message ?? `Ошибка HTTP ${response.status}`);
  }
  return response;
}

export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await request(path, { signal })).json();
}
