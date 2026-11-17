import { useState } from 'react';
import { request } from './api';
import type { Screening } from './talkTypes';

const labels: Record<string, string> = { not_started: 'Отбор ещё не выполнен', queued: 'Отбор в очереди', running: 'Определяется интерес отделов', relevant: 'Есть интерес отделов', not_relevant: 'Пропущен — интерес не найден', uncertain: 'Интерес требует решения', stale: 'Отбор устарел' };

export function Interest({ talkId, value, refresh }: { talkId: string; value: Screening; refresh: () => void }) {
  const [choice, setChoice] = useState<string[] | null>(null);
  const selected = choice ?? value.department_ids;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function save(ids: string[]) {
    setBusy(true); setError('');
    try {
      await request(`/api/v1/talks/${talkId}/screening`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version: value.version, department_ids: ids }) });
      refresh();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  const editable = value.version > 0 && !['queued', 'running', 'stale'].includes(value.state);
  return <details className="interest-details"><summary>{labels[value.state] ?? value.state}</summary>
    {value.stale && <p className="notice">Изменились материалы, положения или модель. Пересчитайте отбор.</p>}
    {value.manual && <p className="muted">Решение оператора</p>}
    {value.items.map(item => <div key={item.department_id}>
      <label className="check"><input type="checkbox" checked={selected.includes(item.department_id)} disabled={!editable || busy}
        onChange={e => setChoice(previous => e.target.checked ? [...(previous ?? value.department_ids), item.department_id] : (previous ?? value.department_ids).filter(id => id !== item.department_id))} />{item.department_name}</label>
      <p>{item.reason}</p>
      {[...item.evidence, ...item.paragraphs].map((quote, index) => <blockquote key={index}><small>{quote.label}</small> {quote.quote}</blockquote>)}
    </div>)}
    {(error || value.error) && <p role="alert">{error || value.error}</p>}
    {editable && <div className="row"><button disabled={busy || !selected.length} onClick={() => void save(selected)}>Подтвердить интерес</button>
      <button disabled={busy} onClick={() => void save([])}>Пропустить доклад</button></div>}
  </details>;
}
