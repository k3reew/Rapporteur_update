import { useCallback, useEffect, useRef, useState } from 'react';
import { getJSON } from './api';

/** Each poll finishes before the next starts; obsolete responses never replace current data. */
export function useResource<T>(url: string | null, interval = 0) {
  const [result, setResult] = useState<{ url: string; value: T }>();
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const refresh = useCallback(() => { generation.current++; setRevision(value => value + 1); }, []);
  const setData = useCallback((value: T) => { generation.current++; setResult({ url: url ?? '', value }); }, [url]);
  useEffect(() => {
    if (!url) return;
    const requestUrl = url;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      const current = generation.current;
      try {
        const value = await getJSON<T>(requestUrl, controller.signal);
        if (!controller.signal.aborted && current === generation.current) { setResult({ url: requestUrl, value }); setError(''); }
      } catch (reason) {
        if (!controller.signal.aborted && current === generation.current) setError((reason as Error).message);
      } finally {
        if (interval && !controller.signal.aborted) timer = setTimeout(() => void load(), interval);
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [url, interval, revision]);
  return { data: result?.url === url ? result.value : undefined, setData, error, refresh };
}
