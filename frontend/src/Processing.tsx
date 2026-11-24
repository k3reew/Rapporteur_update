import { Fragment, useState } from 'react';

import { Interest } from './Interest';
import { request } from './api';
import { useResource } from './useResource';
import { statusTone, talkStatuses } from './status';

import { awaitsOperator } from './talkTypes';
import type { Progress, TalkProgress } from './talkTypes';
import type { Review } from './materialTypes';

const steps: Record<string, string> = {
  screening: 'отбор по отделам',
  parse: 'разбор материалов',
  facts: 'факты и цитаты',
  verify: 'сверка цитат',
  summary: 'текст сводки',
};
const POLL_MS = 5000;

function jobLabel(talk: TalkProgress) {
  if (!talk.job && talk.step === 'screening' && awaitsOperator(talk.screening)) return ['ждёт решения оператора', 'warn'];
  switch (talk.job) {
    case 'queued': return ['в очереди', ''];
    case 'running': return [talk.step ? steps[talk.step] : 'идёт', 'info'];
    case 'done': return ['готово', 'ok'];
    case 'failed': return ['ошибка', 'bad'];
    case 'paused': return ['приостановлено', 'warn'];
    default: return null;
  }
}

export function Processing(
  { conferenceId, onOpen }: { conferenceId: string; onOpen: (talk: TalkProgress) => void },
) {
  const { data: progress, error: loadError, refresh } = useResource<Progress>(`/api/v1/conferences/${conferenceId}/progress`, POLL_MS);
  const { data: materials } = useResource<Review>(progress?.auto_start ? `/api/v1/conferences/${conferenceId}/materials` : null);
  const [error, setError] = useState('');
  const [bulkBusy, setBulkBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [starting, setStarting] = useState(false);
  async function retry(talk: TalkProgress) {
    try {
      await request(`/api/v1/talks/${talk.id}/retry`, { method: 'POST' });
      refresh();
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  /** Пересчёт интереса отделов одного доклада: новая версия отбора ставится в очередь. */
  async function rescreen(talk: TalkProgress) {
    setError('');
    try {
      await request(`/api/v1/talks/${talk.id}/screening`, { method: 'POST' });
      refresh();
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function startProcessing() {
    setStarting(true); setError('');
    try {
      await request(`/api/v1/conferences/${conferenceId}/processing`, { method: 'POST' });
      refresh();
    } catch (reason) {
      setError((reason as Error).message);
    }
    finally { setStarting(false); }
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
  const ready = progress?.talks.filter((talk) => talk.summary_number !== null && talk.summary_status !== 'stale' && !talk.card_only).length ?? 0;
  const cards = progress?.talks.filter((talk) => talk.card_only && talk.job === 'done').length ?? 0;
  const awaiting = progress?.talks.filter((talk) => awaitsOperator(talk.screening)).length ?? 0;
  const reportable = progress?.talks.filter(talk => talk.reportable).length ?? 0;
  const conferenceHash = `#/c/${encodeURIComponent(conferenceId)}`;
  const operator = progress?.workflow === 'operator';
  if (operator && !progress.screening_confirmed) return <section className="card" aria-labelledby="processing-title">
    <h2 id="processing-title">Подготовка сводок</h2>
    <p>Сводки начнут готовиться после подтверждения решений по отделам.</p>
    <a className="primary action-link" href={`${conferenceHash}/screening`}>Проверить соответствие</a>
  </section>;
  return (
    <section className="card" aria-labelledby="processing-title">
      <div className="section-heading">
        <div><h2 id="processing-title">{operator ? 'Подготовка сводок' : 'Проверка докладов'}</h2>
          <p className="muted">{operator ? 'Проверьте готовые аннотации перед выпуском справки.' : 'Сверьте назначения по отделам, затем проверьте черновики сводок.'}</p></div>
        {reportable > 0 && <a className="primary action-link" href={`${conferenceHash}/report`}>Перейти к справке</a>}
      </div>
      <div className="row">
        {operator && <a href={`${conferenceHash}/screening`}>К решениям по отделам</a>}
        {!operator && !!progress?.talks.length && <button disabled={!progress.processing_started} onClick={() => { void request(`/api/v1/conferences/${conferenceId}/screening`, { method: 'POST' }).then(refresh).catch((e: Error) => setError(e.message)); }}>Пересчитать интерес отделов</button>}
        {!progress?.auto_start && !!progress?.talks.some(t => t.bulk_reviewable) && <button disabled={bulkBusy} onClick={() => void confirmAll()}>
          Подтвердить все готовые ({progress.talks.filter(t => t.bulk_reviewable).length})
        </button>}
      </div>
      {!progress && !loadError && <p role="status">Загружаем результаты анализа…</p>}
      {notice && <p role="status">{notice}</p>}
      {(error || loadError) && <p role="alert">{error || loadError}</p>}
      {progress && !progress.processing_started && (
        <div className="notice">
          {progress.auto_start ? <p>Разбор материалов ещё не завершён. <a href={`${conferenceHash}/talks`}>Открыть реестр и ход разбора</a>.</p>
            : <><p>Обработка ещё не запущена. <a href={`${conferenceHash}/talks`}>Проверьте реестр докладов</a>.</p>
              {!!progress.talks.length && <button className="primary" disabled={starting} onClick={() => void startProcessing()}>Запустить обработку</button>}</>}
        </div>
      )}
      {progress?.processing_started && !progress.talks.length && (
        <p className="notice">
          В реестре нет докладов. <a href={`${conferenceHash}/materials`}>Добавьте материалы</a> или
          <a href={`${conferenceHash}/talks`}> настройте привязки докладов</a>.
        </p>
      )}
      {progress?.processing_started && !!progress.talks.length
        && progress.talks.every((talk) => talk.status === 'draft' || talk.status === 'insufficient_data') && (
        <p className="notice">Ни к одному докладу не привязаны материалы: подтвердите доклады на предыдущем этапе.</p>
      )}
      {progress && (
        <>
          {!!progress.talks.length && <div className="kv review-queue">
            <div><b>Готово к выпуску</b>{reportable} из {progress.talks.length}</div>
            <div><b>Файлы</b>{progress.files}</div>
            <div><b>Сводки</b>{ready} из {progress.talks.length}</div>
            {!!cards && <div><b>Только карточка</b>{cards}</div>}
            {!!awaiting && <div><b>Ждут решения по отбору</b>{awaiting}</div>}
          </div>}
          {!!progress.talks.length && <div className="row">
            <div className="progressbar" role="progressbar" aria-label="Доклады, готовые к выпуску" aria-valuenow={reportable} aria-valuemax={progress.talks.length}>
              <span style={{ width: `${100 * reportable / progress.talks.length}%` }} />
            </div>
            <small>
              Сводки готовы у {ready} из {progress.talks.length} докладов{cards ? `, только карточка — у ${cards}` : ''}.
            </small>
          </div>}
          {!!progress.talks.length && <div className="table-wrap">
          <table className="processing-table" aria-label="Доклады и назначения" role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th scope="col" role="columnheader">Доклад</th>
                <th scope="col" role="columnheader">Статус</th>
                <th scope="col" role="columnheader">Обработка</th>
                <th scope="col" role="columnheader"><span hidden>Действие</span></th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {progress.talks.map((talk) => {
                const job = jobLabel(talk);
                const sources = materials?.files.filter(file => file.binding.talk_id === talk.id || file.range_talk_ids?.includes(talk.id));
                return (
                  <Fragment key={talk.id}><tr className="talk-row" role="row">
                    <th scope="row" role="rowheader">
                      {talk.title ?? 'Без названия'}
                      <small className="talk-subline">{talk.section} · {talk.order}</small>
                      {!!talk.speakers?.length && <small className="talk-subline">{talk.speakers.join(', ')}</small>}
                    </th>
                    <td role="cell">
                      <span className={`pill ${statusTone[talk.status] ?? ''}`}>{talk.card_only && talk.status === 'processed' ? 'Карточка готова' : talkStatuses[talk.status] ?? talk.status}</span>
                      {talk.card_only && <><br /><span className="pill">только карточка</span></>}
                      {talk.summary_status === 'needs_attention' && !talk.card_only && <><br /><small>Сводка требует внимания</small></>}
                    </td>
                    <td role="cell">
                      {job ? (
                        <span className={`pill ${job[1]}`}>
                          {talk.job === 'running' && <span className="spinner" aria-hidden="true" />}
                          {job[0]}
                        </span>
                      ) : <span className="muted">—</span>}
                      {talk.error?.startsWith('llm_invalid_output:') ? (
                        <>
                          <small className="talk-subline">Модель вернула ответ в неверном формате.</small>
                          <details className="processing-error">
                            <summary>Подробности ошибки</summary>
                            <small>{talk.error}</small>
                          </details>
                        </>
                      ) : talk.error && <small className="talk-subline">{talk.error}</small>}
                    </td>
                    <td role="cell"><div className="row">
                      {(!progress.auto_start || (talk.summary_number !== null && !talk.card_only))
                        && <button disabled={talk.summary_number === null || talk.card_only} onClick={() => onOpen(talk)}>
                          Открыть сводку
                        </button>}
                      {progress.auto_start && (talk.card_only || talk.screening?.state === 'not_relevant')
                        && <a href={`${conferenceHash}/talks`}>Сверить карточку в реестре</a>}
                      {talk.job === 'failed' && <button onClick={() => void retry(talk)}>Повторить</button>}
                      {(talk.job === 'paused' || talk.screening?.state === 'paused') && <button onClick={() => void retry(talk)}>Продолжить</button>}
                      {!operator && talk.screening && !['queued', 'running'].includes(talk.screening.state)
                        && <button onClick={() => void rescreen(talk)}>Пересчитать отбор</button>}
                    </div></td>
                  </tr>{!operator && talk.screening && <tr className="interest-row" role="row"><td role="cell" colSpan={4}>
                    <Interest key={talk.screening.version} talkId={talk.id} value={talk.screening} refresh={refresh} automatic={progress.auto_start} />
                    {!!sources?.length && <details className="talk-sources">
                      <summary>Оригиналы материалов ({sources.length})</summary>
                      <ul>{sources.map(file => <li key={file.source.id}><a href={`/api/v1/files/${file.source.id}/content`} target="_blank" rel="noreferrer">{file.relative_path}</a></li>)}</ul>
                    </details>}
                  </td></tr>}</Fragment>
                );
              })}
            </tbody>
          </table>
          </div>}
        </>
      )}
    </section>
  );
}
