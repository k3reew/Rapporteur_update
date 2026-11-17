import { useState } from 'react';

import { request } from './api';
import { useResource } from './useResource';
import { statusTone, talkStatuses } from './status';

import type { Progress, TalkProgress } from './talkTypes';

const steps: Record<string, string> = {
  parse: 'разбор материалов',
  facts: 'факты и цитаты',
  verify: 'сверка цитат',
  summary: 'текст сводки',
};
const POLL_MS = 5000;

function jobLabel(talk: TalkProgress) {
  switch (talk.job) {
    case 'queued': return ['в очереди', ''];
    case 'running': return [talk.step ? steps[talk.step] : 'идёт', 'info'];
    case 'done': return ['готово', 'ok'];
    case 'failed': return ['ошибка', 'bad'];
    default: return null;
  }
}

export function Processing(
  { conferenceId, onOpen }: { conferenceId: string; onOpen: (talk: TalkProgress) => void },
) {
  const { data: progress, error: loadError, refresh } = useResource<Progress>(`/api/v1/conferences/${conferenceId}/progress`, POLL_MS);
  const [error, setError] = useState('');
  const [bulkBusy, setBulkBusy] = useState(false);
  const [notice, setNotice] = useState('');
  async function retry(talk: TalkProgress) {
    try {
      await request(`/api/v1/talks/${talk.id}/retry`, { method: 'POST' });
      refresh();
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function confirmAll() {
    const items = (progress?.talks ?? []).filter(t => t.bulk_reviewable)
      .map(t => ({ talk_id: t.id, version: t.summary_number }));
    setBulkBusy(true); setError('');
    try {
      const response = await request(`/api/v1/conferences/${conferenceId}/summaries/review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }),
      });
      const result: { confirmed: string[]; skipped: { talk_id: string; reason: string }[] } = await response.json();
      setNotice(`Подтверждено: ${result.confirmed.length}. ${result.skipped.map(s => s.reason).join('; ')}`);
      refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBulkBusy(false); }
  }
  const ready = progress?.talks.filter((talk) => talk.summary_number !== null).length ?? 0;
  return (
    <section className="card" aria-labelledby="processing-title">
      <h2 id="processing-title">Проверка докладов</h2><p className="muted">Дождитесь обработки, проверьте сводки и подтвердите результаты.</p>
      <button disabled={bulkBusy || !progress?.talks.some(t => t.bulk_reviewable)} onClick={() => void confirmAll()}>
        Подтвердить все готовые ({progress?.talks.filter(t => t.bulk_reviewable).length ?? 0})
      </button>
      {notice && <p role="status">{notice}</p>}
      {(error || loadError) && <p role="alert">{error || loadError}</p>}
      {progress && !progress.processing_started && (
        <p className="notice">Обработка ещё не запущена: подтвердите доклады на предыдущем этапе.</p>
      )}
      {progress?.processing_started && !progress.talks.length && (
        <p className="notice">
          В реестре нет докладов, поэтому сводок не будет. На вкладке «Материалы» добавьте доклады
          или загрузите папку, где у каждого доклада своя подпапка, и подтвердите привязки.
        </p>
      )}
      {progress?.processing_started && !!progress.talks.length
        && progress.talks.every((talk) => talk.status === 'draft' || talk.status === 'insufficient_data') && (
        <p className="notice">Ни к одному докладу не привязаны материалы: подтвердите доклады на предыдущем этапе.</p>
      )}
      {progress && (
        <>
          <div className="kv">
            <div><b>Файлы</b>{progress.files}</div>
            <div><b>Сводки</b>{ready} из {progress.talks.length}</div>
          </div>
          <div className="row">
            <div className="progressbar" aria-hidden="true">
              <span style={{ width: `${progress.talks.length ? (100 * ready) / progress.talks.length : 0}%` }} />
            </div>
            <small>Сводки готовы у {ready} из {progress.talks.length} докладов. Обновляется каждые 5 с.</small>
          </div>
          <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Доклад</th>
                <th scope="col">Статус</th>
                <th scope="col">Обработка</th>
                <th scope="col"><span hidden>Действие</span></th>
              </tr>
            </thead>
            <tbody>
              {progress.talks.map((talk) => {
                const job = jobLabel(talk);
                return (
                  <tr key={talk.id}>
                    <th scope="row">{talk.section} · {talk.order}. {talk.title ?? 'Без названия'}</th>
                    <td>
                      <span className={`pill ${statusTone[talk.status] ?? ''}`}>{talkStatuses[talk.status] ?? talk.status}</span>
                      {talk.summary_status === 'needs_attention' && <><br /><small>Сводка требует внимания</small></>}
                    </td>
                    <td>
                      {job ? (
                        <span className={`pill ${job[1]}`}>
                          {talk.job === 'running' && <span className="spinner" aria-hidden="true" />}
                          {job[0]}
                        </span>
                      ) : <span className="muted">—</span>}
                      {talk.error && <><br /><small>{talk.error}</small></>}
                    </td>
                    <td className="row">
                      <button disabled={talk.summary_number === null} onClick={() => onOpen(talk)}>
                        Открыть сводку
                      </button>
                      {talk.job === 'failed' && <button onClick={() => void retry(talk)}>Повторить</button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        </>
      )}
    </section>
  );
}
