import { Interest } from './Interest';
import type { Screening } from './talkTypes';
import { useState } from 'react';
import { request } from './api';
import { LegacyTalks } from './LegacyTalks';
import { CardEditor, cleanCard, type Speaker } from './TalkCard';
import type { Review } from './materialTypes';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

type Link = { file_id: string; start_ms: number | null; end_ms: number | null; page_start: number | null; page_end: number | null };
type Talk = {
  talk_id: string | null; title: string; speakers: Speaker[]; organizations?: string[]; contacts?: string[];
  links: Link[]; reason: string;
};
type State = { version: number; job: string | null; error: string | null; talks: Talk[]; warnings?: string[] };
const headers = { 'Content-Type': 'application/json' };

export function Preparation({ conferenceId, onConfirmed, onStarted }: { conferenceId: string; onConfirmed: () => void; onStarted?: () => void }) {
  const url = `/api/v1/conferences/${conferenceId}/preparation`;
  const { data: state, setData: setState, error: loadError, refresh } = useResource<State>(url, 5000);
  const { data: review, error: materialsError } = useResource<Review>(`/api/v1/conferences/${conferenceId}/materials`, 5000);
  const { data: interests, refresh: refreshInterests } = useResource<({ talk_id: string } & Screening)[]>(`/api/v1/conferences/${conferenceId}/screening`, 5000);
  const [draft, setDraft] = useState<{ version: number; talks: Talk[] }>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const allowNavigation = useUnsavedChanges(!!draft || busy);
  const files = review?.files ?? [];
  function editTalks(value: Talk[]) {
    setDraft(previous => ({ version: previous?.version ?? state?.version ?? 0, talks: value }));
  }
  const talks = draft?.talks ?? state?.talks ?? [];
  const running = state?.job === 'queued' || state?.job === 'running';
  function update(index: number, changes: Partial<Talk>) {
    editTalks(talks.map((talk, position) => position === index ? { ...talk, ...changes } : talk));
  }
  function split(index: number) {
    const talk = talks[index];
    const link = talk.links[0];
    if (talk.links.length !== 1 || link.start_ms === null || link.end_ms === null) {
      setError('Для разделения оставьте в докладе один временной интервал; остальные материалы перенесите вручную.'); return;
    }
    const middle = Math.floor((link.start_ms + link.end_ms) / 2);
    const copy = [...talks];
    copy.splice(index, 1, { ...talk, links: [{ ...link, end_ms: middle }] },
      { ...talk, talk_id: null, title: `${talk.title} (часть 2, уточните тему)`, links: [{ ...link, start_ms: middle }] });
    editTalks(copy);
  }
  async function startProcessing() {
    await request(`/api/v1/conferences/${conferenceId}/processing`, { method: 'POST' });
    allowNavigation();
    onStarted?.();
  }
  async function act(confirm: boolean) {
    setBusy(true); setError('');
    try {
      if (confirm && (draft || state?.job !== 'confirmed')) {
        const response = await request(url, { method: 'PUT', headers,
          body: JSON.stringify({ version: draft?.version ?? state?.version, talks: talks.map(cleanCard) }) });
        setState(await response.json()); setDraft(undefined); onConfirmed();
      }
      if (confirm) await startProcessing();
      else {
        setState(await (await request(url, { method: 'POST', headers })).json());
        setDraft(undefined);
      }
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  if (!state && !loadError) return <p className="loading">Загрузка докладов…</p>;
  const legacy = state?.job === null && !!review?.talks.length;
  return <section className="workflow-page" aria-label="Подготовка докладов" aria-busy={busy || running}>
    <h2>Проверьте предложенные доклады</h2>
    <p className="muted">Уточните темы, авторов и связанные материалы. Для редактирования откройте доклад.</p>
    {state?.job === 'paused' && <div className="empty-state"><p>Подготовка приостановлена после перезапуска приложения.</p><button disabled={busy || !files.length} onClick={() => void act(false)}>Продолжить подготовку</button></div>}
    {(state?.job === null || state?.job === 'failed') && !legacy && <div className="empty-state"><p>Подготовьте доклады из загруженных материалов.</p><button disabled={busy || !files.length} onClick={() => void act(false)}>Подготовить доклады</button></div>}
    <details className="manual-talks" open={legacy || manualOpen} onToggle={event => setManualOpen(event.currentTarget.open)}><summary>{legacy ? 'Ручной реестр докладов' : 'Добавить доклад вручную и настроить привязки'}</summary><LegacyTalks conferenceId={conferenceId} onStarted={onStarted} enabled={legacy || manualOpen} /></details>
    {running && <p role="status">Подготовка выполняется. Можно закрыть страницу и вернуться позже.</p>}
    {state?.warnings?.map(warning => <p key={warning} className="notice" role="status">{warning}</p>)}
    {(error || loadError || materialsError || state?.error) && <p role="alert">{error || loadError || materialsError || state?.error}</p>}
    {(state?.job === 'done' || state?.job === 'confirmed') && <>
      <p>Предложено докладов: {talks.length}. Время указано в секундах от начала оригинала.</p>
      {talks.map((talk, index) => <details className="talk-editor" key={index}><summary><span><strong>{talk.title || 'Тема не указана'}</strong><small>{talk.speakers.map(s => s.name).join(', ') || 'Автор не установлен'} · материалов {talk.links.length}</small></span><span className="detail-action">Изменить</span></summary><fieldset className="talk-editor-body" disabled={busy || running}>
        <CardEditor card={talk} titleLabel={`Тема доклада ${index + 1}`} onChange={value => update(index, value)} />
        <small>{talk.reason}</small>
        {talk.links.map((link, position) => {
          const file = files.find(f => f.source.id === link.file_id);
          const audio = file?.source.kind === 'audio' || file?.source.kind === 'video';
          const changeLink = (changes: Partial<Link>) => update(index, { links: talk.links.map((l, i) => i === position ? { ...l, ...changes } : l) });
          return <div className="source-binding" key={position}>
            <p>{file?.relative_path ?? link.file_id}</p>
            {audio ? <>
              <div className="row"><label>Начало, с <input type="number" min="0" step="0.001" value={(link.start_ms ?? 0) / 1000}
                onChange={e => changeLink({ start_ms: Math.round(Number(e.target.value) * 1000) })} /></label>
                <label>Конец, с <input type="number" min="0" step="0.001" value={(link.end_ms ?? 0) / 1000}
                  onChange={e => changeLink({ end_ms: Math.round(Number(e.target.value) * 1000) })} /></label></div>
              <audio controls preload="none" src={`/api/v1/files/${link.file_id}/content#t=${(link.start_ms ?? 0) / 1000},${(link.end_ms ?? 0) / 1000}`} />
            </> : <div className="row"><label>Со страницы (пусто — весь файл)<input type="number" min="1" value={link.page_start ?? ''}
              onChange={e => changeLink({ page_start: e.target.value ? Number(e.target.value) : null })} /></label>
              <label>По страницу<input type="number" min="1" value={link.page_end ?? ''}
                onChange={e => changeLink({ page_end: e.target.value ? Number(e.target.value) : null })} /></label></div>}
            <label>Перенести материал в доклад <select value={index} onChange={e => {
              const target = Number(e.target.value);
              editTalks(talks.map((t, i) => i === index ? { ...t, links: t.links.filter((_, j) => j !== position) }
                : i === target ? { ...t, links: [...t.links, link] } : t));
            }}>{talks.map((t, i) => <option value={i} key={i}>{i + 1}. {t.title}</option>)}</select></label>
          </div>;
        })}
        <div className="row"><button onClick={() => split(index)}>Разделить интервал</button>
          <button disabled={index + 1 === talks.length} onClick={() => {
            const copy = [...talks]; copy.splice(index, 2, { ...talk, links: [...talk.links, ...talks[index + 1].links] }); editTalks(copy);
          }}>Объединить со следующим</button>
          <button onClick={() => editTalks(talks.filter((_, i) => i !== index))}>Исключить из разбиения</button></div>
      </fieldset></details>)}
      {state?.job === 'confirmed' && !!interests?.length && <section aria-label="Отбор докладов"><h3>Интерес отделов</h3>
        {interests.map(item => <div key={item.talk_id}><b>{review?.talks.find(talk => talk.id === item.talk_id)?.title ?? 'Доклад'}</b><Interest key={item.version} talkId={item.talk_id} value={item} refresh={refreshInterests} /></div>)}
      </section>}
      <footer className="step-footer"><button className="link" disabled={busy || running || !!draft} onClick={() => void act(false)}>Подготовить заново</button>
      <button className="primary" disabled={busy || running || !talks.length || talks.some(t => !t.links.length || !t.title.trim())}
        onClick={() => void act(true)}>{state?.job === 'confirmed' && !draft ? 'Запустить обработку' : 'Подтвердить и обработать'}</button></footer>
      {draft && <button disabled={busy} onClick={() => { setDraft(undefined); refresh(); }}>Отменить изменения</button>}
    </>}
  </section>;
}
