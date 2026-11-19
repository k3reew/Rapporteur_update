import { useState } from 'react';
import { request } from './api';
import type { Screening } from './talkTypes';

const labels: Record<string, string> = { not_started: 'Отбор ещё не выполнен', queued: 'Отбор в очереди', running: 'Определяется интерес отделов', relevant: 'Есть интерес отделов', not_relevant: 'Интерес не найден — только карточка', uncertain: 'Интерес требует решения', stale: 'Отбор устарел' };

const verdicts: Record<string, string> = { relevant: 'интересен', uncertain: 'нужно решение', not_relevant: 'не интересен' };
type Item = Screening['items'][number];

/** Уверенность модели, что доклад интересен отделу: «85 %»; пусто для решения оператора. */
function share(item: Item) {
  return item.interest === null || item.interest === undefined ? '' : `${item.interest} %`;
}

/** Заголовок: какому отделу интересен доклад и насколько уверена модель. */
function headline(value: Screening) {
  const label = labels[value.state] ?? value.state;
  const interested = value.items.filter(item => item.status === 'relevant');
  return value.state === 'relevant' && interested.length
    ? `${label}: ${interested.map(item => [item.department_name, share(item)].filter(Boolean).join(' — ')).join(', ')}`
    : label;
}

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
  return <details className="interest-details"><summary>{headline(value)}</summary>
    {value.stale && <p className="notice">Изменились материалы, положения или модель. Пересчитайте отбор.</p>}
    {value.manual && <p className="muted">Решение оператора</p>}
    {value.items.map(item => <div key={item.department_id}>
      <label className="check"><input type="checkbox" checked={selected.includes(item.department_id)} disabled={!editable || busy}
        onChange={e => setChoice(previous => e.target.checked ? [...(previous ?? value.department_ids), item.department_id] : (previous ?? value.department_ids).filter(id => id !== item.department_id))} />{item.department_name}</label>
      <small className="muted">{[share(item), verdicts[item.status] ?? item.status].filter(Boolean).join(' · ')}</small>
      <p className="muted">{item.reason}</p>
      {!![...item.evidence, ...item.paragraphs].length && <details><summary>Цитаты</summary>
        {[...item.evidence, ...item.paragraphs].map((quote, index) => <blockquote key={index}><small>{quote.label}</small> {quote.quote}</blockquote>)}</details>}
    </div>)}
    {(error || value.error) && <p role="alert">{error || value.error}</p>}
    {editable && <div className="row"><button disabled={busy || !selected.length} onClick={() => void save(selected)}>Подтвердить интерес</button>
      <button disabled={busy} onClick={() => void save([])}>Только карточка</button></div>}
  </details>;
}
