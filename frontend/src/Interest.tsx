import { useState } from 'react';
import { request } from './api';
import type { Screening } from './talkTypes';

const labels: Record<string, string> = { not_started: 'Отбор ещё не выполнен', queued: 'Отбор в очереди', running: 'Определяется интерес отделов', relevant: 'Есть интерес отделов', not_relevant: 'Интерес не найден — только карточка', uncertain: 'Интерес требует решения', stale: 'Отбор устарел', paused: 'Отбор приостановлен после перезапуска' };

const verdicts: Record<string, string> = { relevant: 'интересен', uncertain: 'нужно решение', not_relevant: 'не интересен' };
const tones: Record<string, string> = { relevant: 'ok', uncertain: 'warn', not_relevant: 'bad' };
type Item = Screening['items'][number];

function scored(item: Item): item is Item & { interest: number } {
  return item.interest !== null && item.interest !== undefined;
}

/** Уверенность модели, что доклад интересен отделу, — отдельной крупной плашкой цвета решения. */
function Score({ item }: { item: Item }) {
  return scored(item)
    ? <span className={`score ${tones[item.status] ?? ''}`} title={`Уверенность модели: ${verdicts[item.status] ?? item.status}`}>{item.interest} %</span>
    : <span className="score" title="Решение оператора или прежней версии отбора">—</span>;
}

/** В свёрнутой строке — отделы, ради которых доклад получил решение: интересные, иначе
 *  требующие решения, иначе самый близкий по уверенности. */
function leaders(value: Screening): Item[] {
  const byInterest = [...value.items].sort((a, b) => (b.interest ?? -1) - (a.interest ?? -1));
  for (const status of ['relevant', 'uncertain']) {
    const found = byInterest.filter(item => item.status === status);
    if (found.length) return found.slice(0, 3);
  }
  return byInterest.filter(scored).slice(0, 1);
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
  const shown = ['relevant', 'uncertain', 'not_relevant'].includes(value.state) && !value.manual ? leaders(value) : [];
  const unscored = !value.manual && !value.error && value.items.length > 0 && !value.items.some(scored);
  return <details className="interest-details"><summary><span>{labels[value.state] ?? value.state}</span>
    {shown.map(item => <span key={item.department_id} className="interest-leader"><Score item={item} /> {item.department_name}</span>)}</summary>
    {value.stale && <p className="notice">Изменились материалы, положения или модель. Пересчитайте отбор.</p>}
    {value.manual && <p className="muted">Решение оператора</p>}
    {unscored && <p className="muted">Процент уверенности появится после «Пересчитать интерес отделов»: это решение получено прежней версией отбора.</p>}
    {value.items.map(item => <div key={item.department_id} className="interest-item">
      <Score item={item} />
      <div>
      <label className="check"><input type="checkbox" checked={selected.includes(item.department_id)} disabled={!editable || busy}
        onChange={e => setChoice(previous => e.target.checked ? [...(previous ?? value.department_ids), item.department_id] : (previous ?? value.department_ids).filter(id => id !== item.department_id))} />{item.department_name}</label>
      <small className="muted"> · {verdicts[item.status] ?? item.status}</small>
      <p className="muted">{item.reason}</p>
      {!![...item.evidence, ...item.paragraphs].length && <details><summary>Цитаты</summary>
        {[...item.evidence, ...item.paragraphs].map((quote, index) => <blockquote key={index}><small>{quote.label}</small> {quote.quote}</blockquote>)}</details>}
      </div>
    </div>)}
    {(error || value.error) && <p role="alert">{error || value.error}</p>}
    {editable && <div className="row"><button disabled={busy || !selected.length} onClick={() => void save(selected)}>Подтвердить интерес</button>
      <button disabled={busy} onClick={() => void save([])}>Только карточка</button></div>}
  </details>;
}
