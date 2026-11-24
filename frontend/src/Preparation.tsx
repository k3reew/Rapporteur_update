import { useState } from 'react';
import { request } from './api';
import { LegacyTalks } from './LegacyTalks';
import { CardEditor, cleanCard, type Speaker } from './TalkCard';
import type { Workflow } from './conferenceTypes';
import type { Review } from './materialTypes';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

type Link = { file_id: string; start_ms: number | null; end_ms: number | null; page_start: number | null; page_end: number | null };
type Talk = {
  talk_id: string | null; title: string; speakers: Speaker[]; organizations?: string[]; contacts?: string[];
  section?: string | null; links: Link[]; reason: string; warnings?: string[]; warnings_ignored?: boolean;
};
type State = {
  version: number; job: string | null; error: string | null; talks: Talk[]; warnings?: string[];
  auto_start?: boolean; workflow?: Workflow; review_reasons?: string[]; context_file_ids?: string[];
};
const headers = { 'Content-Type': 'application/json' };

export function Preparation({ conferenceId, onConfirmed, onStarted }: { conferenceId: string; onConfirmed: (workflow?: Workflow) => void; onStarted?: () => void }) {
  const url = `/api/v1/conferences/${conferenceId}/preparation`;
  const { data: state, setData: setState, error: loadError, refresh } = useResource<State>(url, 5000);
  const { data: review, error: materialsError } = useResource<Review>(`/api/v1/conferences/${conferenceId}/materials`, 5000);
  const [draft, setDraft] = useState<{ version: number; talks: Talk[]; contextIds: string[] }>();
  const [openTalks, setOpenTalks] = useState<ReadonlySet<number>>(new Set());
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const allowNavigation = useUnsavedChanges(!!draft || busy);
  const files = review?.files ?? [];
  const talks = draft?.talks ?? state?.talks ?? [];
  const contextIds = draft?.contextIds ?? state?.context_file_ids ?? [];
  const operator = state?.workflow === 'operator';
  const unassigned = files.filter(file => !['audio', 'video'].includes(file.source.kind)
    && !talks.some(talk => talk.links.some(link => link.file_id === file.source.id)));
  function editTalks(value: Talk[], context = contextIds) {
    setDraft(previous => ({ version: previous?.version ?? state?.version ?? 0, talks: value, contextIds: context }));
  }
  const running = state?.job === 'queued' || state?.job === 'running';
  const stateTalks = state?.talks ?? [];
  /** Правленые доклады: черновик расходится с подтверждённым предложением сервера. */
  const editedTalks = new Set<number>(
    draft ? talks.flatMap((talk, index) => JSON.stringify(talk) === JSON.stringify(stateTalks[index]) ? [] : [index]) : []
  );
  function update(index: number, changes: Partial<Talk>) {
    editTalks(talks.map((talk, position) => position === index ? { ...talk, ...changes } : talk));
  }
  function moveTalk(from: number, to: number) {
    const indices = talks.map((_, index) => index);
    indices.splice(from, 1);
    indices.splice(to, 0, from);
    setOpenTalks(new Set(indices.flatMap((index, position) => openTalks.has(index) ? [position] : [])));
    editTalks(indices.map(index => talks[index]));
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
  function separate(index: number, position: number) {
    const talk = talks[index];
    const link = talk.links[position];
    setOpenTalks(new Set([...openTalks, talks.length]));
    editTalks([...talks.map((item, i) => i === index ? { ...item, links: item.links.filter((_, j) => j !== position) } : item),
      { talk_id: null, title: '', speakers: [], section: talk.section, links: [link], reason: '' }]);
  }
  function merge(index: number, target: number) {
    const original = talks[index];
    const distinct = <T,>(items: T[], key: (item: T) => string) => [...new Map(items.map(item => [key(item), item])).values()];
    setOpenTalks(new Set());
    editTalks(talks.map((talk, i) => i === target ? {
      ...talk, links: distinct([...original.links, ...talk.links], link => JSON.stringify(link)),
      speakers: distinct([...original.speakers, ...talk.speakers], speaker => speaker.name.trim().toLocaleLowerCase('ru')),
      organizations: [...new Set([...(original.organizations ?? []), ...(talk.organizations ?? [])])],
      contacts: [...new Set([...(original.contacts ?? []), ...(talk.contacts ?? [])])],
    } : talk).filter((_, i) => i !== index));
  }
  function assign(fileId: string, selection: string) {
    if (!selection) return;
    if (selection === 'context') {
      editTalks(talks.map(talk => ({ ...talk, links: talk.links.filter(link => link.file_id !== fileId) }))
        .filter(talk => talk.links.length), [...new Set([...contextIds, fileId])]);
      return;
    }
    const link: Link = { file_id: fileId, start_ms: null, end_ms: null, page_start: null, page_end: null };
    const remaining = contextIds.filter(id => id !== fileId);
    if (selection === 'new') {
      const file = files.find(item => item.source.id === fileId);
      const title = file?.title_candidates?.title ?? '';
      const speakers = (file?.title_candidates?.speakers ?? []).map(name => ({ name }));
      setOpenTalks(new Set([...openTalks, talks.length]));
      editTalks([...talks, { talk_id: null, title, speakers, links: [link], reason: '' }], remaining);
    } else if (selection === 'unassigned') {
      editTalks(talks, remaining);
    } else {
      editTalks(talks.map((talk, index) => index === Number(selection) ? { ...talk, links: [...talk.links, link] } : talk), remaining);
    }
  }
  /** Развёрнутый доклад выделяется цветом; название поднимается к верхней границе экрана. */
  function toggleTalk(index: number, event: { currentTarget: HTMLDetailsElement }) {
    const open = event.currentTarget.open;
    setOpenTalks(previous => {
      const next = new Set(previous);
      if (open) next.add(index); else next.delete(index);
      return next;
    });
    const element = event.currentTarget;
    if (open && typeof element.scrollIntoView === 'function') {
      const reduceMotion = typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      element.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
    }
  }
  async function reprepare(force = false) {
    setBusy(true); setError('');
    try {
      setState(await (await request(url, {
        method: 'POST', headers,
        ...(state?.auto_start || operator ? { body: JSON.stringify({
          ...(operator ? { workflow: 'operator' } : {}), auto_start: true, ...(force ? { force } : {}),
        }) } : {}),
      })).json());
      setDraft(undefined); setOpenTalks(new Set());
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError('');
    try {
      const response = await request(url, { method: 'PUT', headers,
        body: JSON.stringify({
          version: draft?.version ?? state?.version,
          talks: talks.map(talk => cleanCard({ ...talk, ...(operator ? { section: talk.section?.trim() || null } : {}) })),
          ...(operator ? { context_file_ids: contextIds } : {}),
        }) });
      setState(await response.json()); setDraft(undefined);
      allowNavigation();
      onConfirmed(state?.workflow);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  async function startProcessing() {
    setBusy(true); setError('');
    try {
      await request(`/api/v1/conferences/${conferenceId}/processing`, { method: 'POST' });
      allowNavigation();
      onStarted?.();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  if (!state && !loadError) return <p className="loading">Загрузка докладов…</p>;
  if (state?.job === 'awaiting_start' && !draft) return <section className="workflow-page">
    <h2>Материалы изменились</h2>
    <p>Проверьте список файлов и начните анализ материалов.</p>
    <a className="primary action-link" href={`#/c/${encodeURIComponent(conferenceId)}/materials`}>К материалам</a>
  </section>;
  const legacy = state?.job === null && !!review?.talks.length;
  const talkInvalid = busy || running || !talks.length || talks.some(t => !t.links.length || !t.title.trim());
  return <section className="workflow-page" aria-label="Подготовка докладов" aria-busy={busy || running}>
    <h2>{running ? 'Разбор материалов' : 'Реестр докладов'}</h2>
    {state?.job === 'done' && <p className="muted">Откройте доклад, чтобы сверить тему, авторов и привязки материалов.</p>}
    {state?.job === 'done' && !!state.review_reasons?.length && <div className="notice" role="status">
      <ul>{state.review_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
    </div>}
    {state?.job === 'confirmed' && state.auto_start && !draft && <div className="next-action">
      <div><h3>Реестр готов</h3><p>{operator ? 'Теперь можно проверить соответствие докладов задачам отделов.' : 'На этапе проверки видны назначения по отделам и черновики сводок.'}</p></div>
      <button className="primary" onClick={() => onConfirmed(state.workflow)}>{operator ? 'Перейти к соответствию' : 'Перейти к проверке'}</button>
    </div>}
    {state?.job === 'paused' && <div className="empty-state"><p>Подготовка приостановлена после перезапуска приложения.</p><button disabled={busy || !files.length} onClick={() => void reprepare()}>Продолжить подготовку</button></div>}
    {(state?.job === null || state?.job === 'failed') && !legacy && <div className="empty-state">
      <p>{state.job === 'failed' ? 'Разбор не завершился. Можно попробовать ещё раз.' : 'Добавьте материалы и начните анализ.'}</p>
      {!files.length || state.job === null
        ? <a href={`#/c/${encodeURIComponent(conferenceId)}/materials`}>К материалам</a>
        : <button disabled={busy} onClick={() => void reprepare()}>Повторить разбор</button>}
    </div>}
    {!running && !operator && <details className="manual-talks" open={legacy || manualOpen} onToggle={event => setManualOpen(event.currentTarget.open)}><summary>{legacy ? 'Ручной реестр докладов' : 'Добавить доклад вручную и настроить привязки'}</summary><LegacyTalks conferenceId={conferenceId} onStarted={onStarted} enabled={legacy || manualOpen} /></details>}
    {running && <div className="empty-state">
      <p role="status">{state?.job === 'queued' ? 'Материалы в очереди на разбор.' : 'Извлекаем содержание файлов и выделяем доклады.'}</p>
      <p className="muted">Анализ продолжится, если вы закроете вкладку.</p>
    </div>}
    {state?.warnings?.map(warning => <p key={warning} className="notice" role="status">{warning}</p>)}
    {(error || loadError || materialsError || state?.error) && <p role="alert">{error || loadError || materialsError || state?.error}</p>}
    {(state?.job === 'done' || state?.job === 'confirmed') && <>
      <p>Докладов в реестре — {talks.length}.</p>
      {!talks.length && <p className="notice">Назначьте материал докладом или <a href={`#/c/${encodeURIComponent(conferenceId)}/materials`}>загрузите дополнительные материалы</a>.</p>}
      {operator && <datalist id="preparation-sections">{review?.sections.map(section => <option key={section.id} value={section.title} />)}</datalist>}
      {talks.map((talk, index) => <div className={`talk-row${talk.warnings?.length ? ' has-warning' : ''}`} key={index}>
        {!!talk.warnings?.length && <div className="talk-warning">
          <button type="button" className={talk.warnings_ignored ? 'text-ok' : 'text-warn'}
            aria-label={talk.warnings_ignored ? 'Предупреждение отмечено как проверенное' : 'Предупреждение о докладе'}
            aria-describedby={`talk-warning-${index}`}>
            <span aria-hidden="true">{talk.warnings_ignored ? '✓' : '⚠'}</span>
          </button>
          <div className="talk-warning-popover">
            <ul id={`talk-warning-${index}`}>{talk.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>
            <label className="check"><input type="checkbox" checked={!!talk.warnings_ignored} disabled={busy || running}
              onChange={event => update(index, { warnings_ignored: event.target.checked })} /> Отметить как проверенное</label>
          </div>
        </div>}
        <details
        className={`talk-editor${openTalks.has(index) ? ' open' : ''}`}
        open={openTalks.has(index)}
        onToggle={event => toggleTalk(index, event)}
      ><summary><span><span>{index + 1}. </span><strong>{talk.title || 'Тема не указана'}</strong><small>{talk.speakers.map(s => s.name).join(', ') || 'Автор не установлен'} · материалов {talk.links.length}</small></span>
        <span className="row">{editedTalks.has(index) && <span className="edited-mark" title="Карточка изменена: правки ещё не сохранены">изменён</span>}<span className="detail-action">Изменить</span></span></summary><fieldset className="talk-editor-body" disabled={busy || running}>
        {talks.length > 1 && <label>Порядковый номер доклада
          <select value={index} onChange={event => moveTalk(index, Number(event.target.value))}>
            {talks.map((_, position) => <option key={position} value={position}>{position + 1}</option>)}
          </select>
        </label>}
        <CardEditor card={talk} titleLabel={`Тема доклада ${index + 1}`} onChange={value => update(index, value)} />
        {operator && <label className="field">Секция<input list="preparation-sections" value={talk.section ?? ''}
          onChange={event => update(index, { section: event.target.value })} /></label>}
        {talk.links.map((link, position) => {
          const file = files.find(f => f.source.id === link.file_id);
          const audio = file?.source.kind === 'audio' || file?.source.kind === 'video';
          const changeLink = (changes: Partial<Link>) => update(index, { links: talk.links.map((l, i) => i === position ? { ...l, ...changes } : l) });
          return <div className="source-binding" key={position}>
            <p><a href={`/api/v1/files/${link.file_id}/content`} target="_blank" rel="noreferrer">{file?.relative_path ?? 'Оригинал материала'}</a></p>
            {audio ? <>
              <div className="row"><label>Начало, с <input type="number" min="0" step="0.001" value={(link.start_ms ?? 0) / 1000}
                onChange={e => changeLink({ start_ms: Math.round(Number(e.target.value) * 1000) })} /></label>
                <label>Конец, с <input type="number" min="0" step="0.001" value={(link.end_ms ?? 0) / 1000}
                  onChange={e => changeLink({ end_ms: Math.round(Number(e.target.value) * 1000) })} /></label></div>
            </> : <div className="row"><label>Со страницы (пусто — весь файл)<input type="number" min="1" value={link.page_start ?? ''}
              onChange={e => changeLink({ page_start: e.target.value ? Number(e.target.value) : null })} /></label>
              <label>По страницу<input type="number" min="1" value={link.page_end ?? ''}
                onChange={e => changeLink({ page_end: e.target.value ? Number(e.target.value) : null })} /></label></div>}
            <label className="field">Перенести материал <select value="" onChange={e => {
              const destination = e.target.value;
              if (destination === 'context') { assign(link.file_id, destination); return; }
              if (destination === 'new') { separate(index, position); return; }
              const target = Number(destination);
              setOpenTalks(new Set());
              editTalks(talks.map((t, i) => i === index ? { ...t, links: t.links.filter((_, j) => j !== position) }
                : i === target ? { ...t, links: [...t.links, link] } : t).filter(t => t.links.length));
            }}><option value="" disabled>Выберите назначение</option>
              {talks.map((t, i) => i !== index && <option value={i} key={i}>{i + 1}. {t.title || 'Доклад без названия'}</option>)}
              {operator && talk.links.length > 1 && <option value="new">Выделить в отдельный доклад</option>}
              {operator && <option value="context">Материал конференции</option>}
            </select></label>
          </div>;
        })}
        <div className="row">{!operator && <button onClick={() => split(index)}>Разделить интервал</button>}
          {talks.length > 1 && <label className="field">Объединить с докладом<select value="" onChange={event => merge(index, Number(event.target.value))}>
            <option value="" disabled>Выберите доклад</option>
            {talks.map((candidate, i) => i !== index && <option key={i} value={i}>{i + 1}. {candidate.title || 'Доклад без названия'}</option>)}
          </select></label>}
          <button onClick={() => editTalks(talks.filter((_, i) => i !== index))}>Исключить из разбиения</button></div>
      </fieldset></details></div>)}
      {operator && !!unassigned.length && <section className="unbound-materials" aria-labelledby="unbound-title">
        <h3 id="unbound-title">Материалы вне докладов</h3>
        <p className="muted">Программу и организационные документы можно оставить материалами конференции.</p>
        {unassigned.map(file => {
          const id = file.source.id;
          return <div className="unbound-material" key={id}>
            <a href={`/api/v1/files/${id}/content`} target="_blank" rel="noreferrer">{file.relative_path}</a>
            <select aria-label={`Роль ${file.relative_path}`} disabled={busy || running}
              value={contextIds.includes(id) ? 'context' : 'unassigned'} onChange={event => assign(id, event.target.value)}>
              <option value="unassigned">Не включать</option>
              <option value="context">Материал конференции</option>
              {file.source.kind !== 'program' && <>
                <option value="new">Новый доклад</option>
                {talks.map((talk, i) => <option key={i} value={i}>К докладу {i + 1}. {talk.title || 'Доклад без названия'}</option>)}
              </>}
            </select>
            {file.source.kind === 'program' && <a className="muted" href={`#/c/${encodeURIComponent(conferenceId)}/materials`}>Изменить тип программы</a>}
          </div>;
        })}
      </section>}
      <footer className="step-footer"><button className="link" disabled={busy || running || !!draft} onClick={() => void reprepare(true)}>Подготовить заново</button>
      <span className="row">
        {(draft || state?.job !== 'confirmed') && <button className="primary" disabled={talkInvalid}
          onClick={() => void save()}>{operator ? 'Подтвердить реестр' : state?.auto_start ? 'Подтвердить и продолжить анализ' : 'Подтвердить'}</button>}
        {state?.job === 'confirmed' && !draft && !state.auto_start && <button className="primary" disabled={busy || running}
          onClick={() => void startProcessing()}>Запустить обработку</button>}
      </span></footer>
      {draft && <button disabled={busy} onClick={() => { setDraft(undefined); setOpenTalks(new Set()); refresh(); }}>Отменить изменения</button>}
    </>}
  </section>;
}
