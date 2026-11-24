import { useState } from 'react';
import { request } from './api';
import { awaitsOperator, type Screening } from './talkTypes';

const proposals: Record<string, string> = { relevant: 'Модель: есть интерес — подтвердите решение', not_relevant: 'Модель: интереса нет — подтвердите решение', uncertain: 'Интерес требует решения' };
const labels: Record<string, string> = { not_started: 'Отбор ещё не выполнен', queued: 'Отбор в очереди', running: 'Определяется интерес отделов', relevant: 'Есть интерес отделов', not_relevant: 'Интерес не найден — только карточка', uncertain: 'Интерес требует решения', stale: 'Отбор устарел', paused: 'Отбор приостановлен после перезапуска' };

const verdicts: Record<string, string> = { relevant: 'интересен', uncertain: 'нужно решение', not_relevant: 'не интересен' };
const tones: Record<string, string> = { relevant: 'ok', uncertain: 'warn', not_relevant: 'bad' };
type Item = Screening['items'][number];

function scored(item: Item): item is Item & { interest: number } {
  return item.interest !== null && item.interest !== undefined;
}

/** Уверенность модели, что доклад интересен отделу, — отдельной крупной плашкой цвета решения. */
function Score({ item, manual }: { item: Item; manual: boolean }) {
  return scored(item)
    ? <span className={manual ? 'score' : `score ${tones[item.status] ?? ''}`} title="Оценка модели">{item.interest} %</span>
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

export function Interest({ talkId, value, refresh, automatic, sourceOf, decision }: {
  talkId: string;
  value: Screening;
  refresh: () => void;
  automatic?: boolean;
  sourceOf?: (label: string) => string | undefined;
  decision?: { selected: string[]; unresolved: boolean; disabled: boolean; update: (ids: string[]) => void };
}) {
  const [choice, setChoice] = useState<string[] | null>(null);
  const selected = decision?.selected ?? choice ?? value.department_ids;
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
  const editable = value.version > 0 && !['queued', 'running', 'stale', 'paused'].includes(value.state);
  const shown = ['relevant', 'uncertain', 'not_relevant'].includes(value.state) ? leaders(value) : [];
  const unscored = !value.manual && !value.error && value.items.length > 0 && !value.items.some(scored);
  const awaiting = awaitsOperator(value);
  const requiresDecision = decision?.unresolved ?? awaiting;
  return <details className="interest-details" open={requiresDecision || undefined}><summary><span className={requiresDecision ? 'text-warn' : undefined}>{(awaiting ? proposals[value.state] : undefined) ?? labels[value.state] ?? value.state}</span>
    {shown.map(item => <span key={item.department_id} className="interest-leader"><Score item={item} manual={value.manual} /> {item.department_name}</span>)}</summary>
    {value.stale && <p className="notice">Изменились материалы, положения или модель. Пересчитайте отбор.</p>}
    {value.manual && <p className="muted">Решение оператора</p>}
    {requiresDecision && <p className="notice">
      Модель только предлагает решение и может ошибаться. {value.error
        ? 'Отбор не завершился. Проверьте материалы и положения или повторите расчёт.'
        : 'Сверьте цитаты доклада и положения. Выберите отделы или сохраните доклад как карточку.'}
    </p>}
    {unscored && <p className="muted">Процент уверенности появится после «Пересчитать интерес отделов»: это решение получено прежней версией отбора.</p>}
    {value.items.map(item => <div key={item.department_id} className="interest-item">
      <Score item={item} manual={value.manual} />
      <div>
      <label className="check"><input type="checkbox" checked={selected.includes(item.department_id)} disabled={!editable || busy || decision?.disabled}
        onChange={e => {
          const ids = e.target.checked ? [...selected, item.department_id] : selected.filter(id => id !== item.department_id);
          if (decision) decision.update(ids); else setChoice(ids);
        }} />{item.department_name}</label>
      <small className="muted"> · {verdicts[item.status] ?? item.status}</small>
      <p className="muted">{item.reason}</p>
      {!![...item.evidence, ...item.paragraphs].length && <details><summary>Цитаты</summary>
        {item.evidence.map((quote, index) => {
          const source = quote.source_url || sourceOf?.(quote.label);
          return <blockquote key={index}>
            <small>{source ? <a href={source} target="_blank" rel="noreferrer">Источник доклада {quote.label}</a> : `Доклад ${quote.label}`}</small> {quote.quote}
          </blockquote>;
        })}
        {item.paragraphs.map((quote, index) => <blockquote key={index}>
          <small>{quote.source_url ? <a href={quote.source_url} target="_blank" rel="noreferrer">Источник отдела {quote.label}</a> : `Положение отдела ${quote.label}`}</small> {quote.quote}
        </blockquote>)}</details>}
      </div>
    </div>)}
    {(error || value.error) && <p role="alert">{error || value.error}</p>}
    {editable && (!decision || requiresDecision) && <div className="row">
      <button disabled={busy || !selected.length || decision?.disabled}
        onClick={() => decision ? decision.update(selected) : void save(selected)}>{decision ? 'Подтвердить выбор отделов' : 'Подтвердить интерес'}</button>
      <button disabled={busy || decision?.disabled}
        onClick={() => decision ? decision.update([]) : void save([])}>{automatic || decision ? 'Оставить карточку' : 'Только карточка'}</button>
    </div>}
  </details>;
}
