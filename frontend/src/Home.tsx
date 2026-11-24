import { useState } from 'react';

import { request } from './api';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';

import { departmentReady, type Department } from './Departments';
import type { ConferenceItem, ConferenceStep } from './conferenceTypes';

function period(item: ConferenceItem) {
  const format = (value: string) => new Date(`${value}T00:00:00`).toLocaleDateString('ru-RU');
  if (!item.start_date) return 'даты не указаны';
  if (!item.end_date || item.end_date === item.start_date) return format(item.start_date);
  return `${format(item.start_date)} – ${format(item.end_date)}`;
}

const stepLabels: Record<ConferenceStep, string> = {
  materials: 'Загрузка материалов',
  talks: 'Разбор материалов',
  screening: 'Соответствие отделам',
  review: 'Подготовка сводок',
  report: 'Справка',
};

export function Home({ onOpen }: { onOpen: (conferenceId: string, step: ConferenceStep) => void }) {
  const { data: conferences, setData: setConferences, error: loadError } = useResource<ConferenceItem[]>('/api/v1/conferences');
  const { data: departments, error: departmentError, refresh: refreshDepartments } = useResource<Department[]>('/api/v1/departments');
  const readyCount = departments?.filter(departmentReady).length ?? 0;
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');

  const allowNavigation = useUnsavedChanges(!!title.trim() || !!busy);

  async function create() {
    setBusy('Создание…');
    setError('');
    try {
      const response = await request('/api/v1/conferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim(), workflow: 'operator' }),
      });
      const conference: ConferenceItem = await response.json();
      setTitle('');
      allowNavigation(); onOpen(conference.id, 'materials');
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy('');
    }
  }

  async function remove(item: ConferenceItem) {
    const name = item.title ?? 'Без названия';
    if (!window.confirm(`Удалить конференцию «${name}» вместе с реестром, сводками и справками? Отменить удаление нельзя.`)) return;
    setBusy('Удаление…');
    setError('');
    setStatus('');
    try {
      await request(`/api/v1/conferences/${item.id}`, { method: 'DELETE' });
      setConferences((conferences ?? []).filter(({ id }) => id !== item.id));
      setStatus(`Конференция «${name}» удалена.`);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy('');
    }
  }

  return (
    <section aria-labelledby="home-title" aria-busy={!!busy}>
      <h2 id="home-title">Конференции</h2>
      {departments && !readyCount && <div className="next-action">
        <div><h3>Добавьте отделы</h3>
          <p>Укажите функции отделов или приложите положения для отбора докладов.</p></div>
        <a className="primary action-link" href="#/departments">Добавить отдел</a>
      </div>}
      {!departments && !departmentError && <p role="status">Загружаем отделы…</p>}
      {departmentError && <div className="row">
        <p role="alert">Сведения об отделах недоступны. {departmentError}</p>
        <button onClick={refreshDepartments}>Повторить</button>
      </div>}
      {!!readyCount && <div className="next-action home-create">
        <div><h3>Новая конференция</h3>
          <p>Отделов готово к анализу — {readyCount}. <a href="#/departments">Управление отделами</a></p></div>
        <form className="row" onSubmit={(event) => { event.preventDefault(); if (title.trim()) void create(); }}>
          <label className="field">
            <span>Название новой конференции</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} required />
          </label>
          <button className="primary" disabled={!!busy || !title.trim()}>Создать</button>
        </form>
      </div>}
      <p role="status">{busy || status}</p>
      {(error || loadError) && <p role="alert">{error || loadError}</p>}
      {!conferences && !loadError && <p className="loading">Загрузка конференций…</p>}
      {conferences && !conferences.length && <p className="muted">Конференций пока нет.</p>}
      <div className="conference-list">
        {conferences?.map((item) => {
          const step = item.resume_step ?? (item.processing_started ? 'review' : 'materials');
          return (
            <article key={item.id} className="card" aria-label={item.title ?? 'Без названия'}>
              <h3>{item.title ?? 'Без названия'}</h3>
              <span className="muted">{period(item)}{item.place && ` · ${item.place}`}</span>
              <div className="kv">
                <div><b>Доклады</b>{item.talks}</div>
                <div><b>Сводки</b>{item.summaries}</div>
                <div><b>Проверено</b>{item.reviewed}</div>
              </div>
              <span className="pill info">{stepLabels[step]}</span>
              <div className="row">
                <button className="primary" onClick={() => onOpen(item.id, step)}>Продолжить</button>
                <button className="danger" disabled={!!busy} onClick={() => void remove(item)}>Удалить</button>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
