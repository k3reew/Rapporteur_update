import { useState } from 'react';

import { Interest } from './Interest';
import { request } from './api';
import type { Review } from './materialTypes';
import type { Progress, Screening } from './talkTypes';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

type Decision = { talk_id: string; version: number; department_ids: string[] };
const waitingStates = ['not_started', 'queued', 'running', 'stale', 'paused'];

function needsDecision(value: Screening) {
  return !value.manual && (value.state === 'uncertain' || !!value.error || value.items.some(item => item.status === 'uncertain'));
}

export function ConferenceScreening({ conferenceId, onConfirmed }: { conferenceId: string; onConfirmed: () => void }) {
  const base = `/api/v1/conferences/${conferenceId}`;
  const { data: progress, error: loadError, refresh } = useResource<Progress>(`${base}/progress`, 3000);
  const { data: materials } = useResource<Review>(progress?.processing_started ? `${base}/materials` : null);
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const dirty = Object.keys(decisions).length > 0;
  const allowNavigation = useUnsavedChanges(dirty || busy);
  const talks = progress?.talks ?? [];
  const validChoice = (id: string, value: Screening) => decisions[id]?.version === value.version ? decisions[id] : undefined;
  const waiting = talks.filter(({ screening }) => !screening || waitingStates.includes(screening.state)).length;
  const unresolved = talks.filter(({ id, screening }) => screening && !waitingStates.includes(screening.state)
    && ((decisions[id] && !validChoice(id, screening)) || (needsDecision(screening) && !validChoice(id, screening)))).length;
  const canConfirm = !!progress?.processing_started && !!talks.length && !waiting && !unresolved && (!progress.screening_confirmed || dirty);
  const conferenceHash = `#/c/${encodeURIComponent(conferenceId)}`;

  function discard(id: string) {
    setDecisions(previous => {
      const remaining = { ...previous };
      delete remaining[id];
      return remaining;
    });
  }

  async function retry(id: string) {
    setBusy(true); setError('');
    try {
      await request(`/api/v1/talks/${id}/screening`, { method: 'POST' });
      discard(id);
      refresh();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  async function confirm() {
    const results = talks.flatMap(({ id, screening }) => {
      if (!screening) return [];
      const choice = validChoice(id, screening);
      if (choice) return [choice];
      return screening.manual ? [] : [{ talk_id: id, version: screening.version, department_ids: screening.department_ids }];
    });
    setBusy(true); setError('');
    try {
      await request(`${base}/screening`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ talks: results }),
      });
      setDecisions({});
      allowNavigation();
      onConfirmed();
    } catch (reason) { setError((reason as Error).message); refresh(); }
    finally { setBusy(false); }
  }

  return <section className="workflow-page" aria-labelledby="screening-title" aria-busy={busy}>
    <div className="section-heading">
      <div><h2 id="screening-title">Соответствие отделам</h2>
        <p className="muted">Сверьте оценки и цитаты. После подтверждения подготовятся аннотации для докладов с выбранными отделами.</p></div>
      {!!progress?.screening_confirmed && !dirty && <button className="primary" onClick={onConfirmed}>Перейти к сводкам</button>}
    </div>
    {!progress && !loadError && <p role="status">Загружаем оценки…</p>}
    {(loadError || error) && <p role="alert">{loadError || error}</p>}
    {progress && (!progress.processing_started || !talks.length) && <div className="notice">
      <p>Сначала подтвердите реестр докладов.</p>
      <a href={`${conferenceHash}/talks`}>Открыть реестр</a>
    </div>}
    {!!progress?.processing_started && !!talks.length && <>
      {(waiting > 0 || unresolved > 0) && <p role="status">
        {waiting > 0 && `Оценка ещё не готова для ${waiting} из ${talks.length}. `}
        {unresolved > 0 && `Требуют решения — ${unresolved}.`}
      </p>}
      <div className="screening-list">
        {talks.map(talk => {
          const value = talk.screening;
          const needsRetry = !value || ['not_started', 'paused', 'stale'].includes(value.state) || !!value.error;
          const sources = materials?.files?.filter(file => file.binding?.talk_id === talk.id || file.range_talk_ids?.includes(talk.id));
          const choice = value && validChoice(talk.id, value);
          const changed = !!decisions[talk.id] && !choice;
          return <article key={talk.id} className="screening-talk">
            <h3>{talk.title || 'Доклад без названия'}</h3>
            {!!talk.speakers?.length && <p className="muted">{talk.speakers.join(', ')}</p>}
            {value ? <Interest key={value.version} value={value} talkId={talk.id} refresh={refresh}
              decision={{
                selected: choice?.department_ids ?? value.department_ids,
                unresolved: needsDecision(value) && !choice,
                disabled: busy,
                update: (department_ids) => setDecisions(previous => ({
                  ...previous, [talk.id]: { talk_id: talk.id, version: value.version, department_ids },
                })),
              }} /> : <p className="muted">Оценка ещё не выполнена.</p>}
            {!!choice && <p className="muted">{choice.department_ids.length ? 'Выбраны отделы.' : 'Выбрана карточка.'} Решение сохранится при общем подтверждении.</p>}
            {changed && <div className="notice">Оценка обновилась. Сверьте решение заново. <button disabled={busy} onClick={() => discard(talk.id)}>Сбросить прежний выбор</button></div>}
            <div className="row">
              {needsRetry && <button disabled={busy} onClick={() => void retry(talk.id)}>Повторить оценку</button>}
              {!!sources?.length && <details className="talk-sources">
                <summary>Оригиналы материалов</summary>
                <ul>{sources.map(file => <li key={file.source.id}><a href={`/api/v1/files/${file.source.id}/content`} target="_blank" rel="noreferrer">{file.relative_path}</a></li>)}</ul>
              </details>}
            </div>
          </article>;
        })}
      </div>
      {(!progress.screening_confirmed || dirty) && <footer className="step-footer">
        <button className="primary" disabled={busy || !canConfirm} onClick={() => void confirm()}>Подтвердить решения</button>
      </footer>}
    </>}
  </section>;
}
