import { useEffect, useState } from 'react';

import { Departments } from './Departments';
import { Home } from './Home';
import type { ConferenceItem } from './conferenceTypes';
import { Materials } from './Materials';
import { Preparation } from './Preparation';
import { useResource } from './useResource';
import { Processing } from './Processing';
import { ReportPanel } from './ReportPanel';
import { Settings } from './Settings';
import { SummaryReview } from './SummaryReview';
import { applyTheme, effectiveTheme, storedTheme, type Theme } from './theme';

type Tab = 'materials' | 'talks' | 'review' | 'report';
type Route =
  | { page: 'home' }
  | { page: 'settings' }
  | { page: 'departments' }
  | { page: 'conference'; id: string; tab: Tab }
  | { page: 'talk'; id: string; talkId: string };

const tabs: [Tab, string][] = [['materials', 'Материалы'], ['talks', 'Доклады'], ['review', 'Проверка'], ['report', 'Справка']];

/** Адрес страницы в hash: работают «назад», перезагрузка и ссылки на доклад. */
export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[0] === 'departments') return { page: 'departments' };
  if (parts[0] === 'settings') return { page: 'settings' };
  if (parts[0] === 'c' && parts[1]) {
    if (parts[2] === 'talk' && parts[3]) return { page: 'talk', id: parts[1], talkId: parts[3] };
    const tab = parts[2] === 'processing' ? 'review' : tabs.find(([value]) => value === parts[2])?.[0] ?? 'materials';
    return { page: 'conference', id: parts[1], tab };
  }
  return { page: 'home' };
}

export function routeHash(route: Route): string {
  switch (route.page) {
    case 'home': return '#/';
    case 'settings': return '#/settings';
    case 'departments': return '#/departments';
    case 'conference': return `#/c/${encodeURIComponent(route.id)}/${route.tab}`;
    case 'talk': return `#/c/${encodeURIComponent(route.id)}/talk/${encodeURIComponent(route.talkId)}`;
  }
}

function go(route: Route) {
  const hash = routeHash(route);
  if (hash !== routeHash(parseRoute(window.location.hash))) window.location.hash = hash;
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [theme, setTheme] = useState<Theme>(storedTheme);
  const conferenceId = route.page === 'conference' || route.page === 'talk' ? route.id : '';
  const { data: conference, error } = useResource<ConferenceItem>(conferenceId ? `/api/v1/conferences/${conferenceId}` : null);

  useEffect(() => {
    const update = () => {
      const hash = window.location.hash;
      if (routeHash(parseRoute(hash)) === routeHash(route)) return;
      if (window.dispatchEvent(new CustomEvent('rapporteur-navigate', { detail: hash, cancelable: true }))) setRoute(parseRoute(hash));
      else window.history.replaceState(null, '', routeHash(route));
    };
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, [route]);

  useEffect(() => applyTheme(theme), [theme]);

  const dark = effectiveTheme(theme) === 'dark';
  return (
    <>
      <header className="app-header">
        <h1><a href="#/">Репортёр</a></h1>
        
        <nav aria-label="Разделы приложения">
          <button aria-current={route.page === 'home' ? 'page' : undefined} onClick={() => go({ page: 'home' })}>
            Конференции
          </button>
          <button aria-current={route.page === 'settings' ? 'page' : undefined} onClick={() => go({ page: 'settings' })}>
            Настройки
          </button>
          <button aria-current={route.page === 'departments' ? 'page' : undefined} onClick={() => go({ page: 'departments' })}>Отделы</button>
        </nav>
        <button
          className="theme-toggle"
          aria-label={dark ? 'Включить светлую тему' : 'Включить тёмную тему'}
          title={dark ? 'Светлая тема' : 'Тёмная тема'}
          onClick={() => setTheme(dark ? 'light' : 'dark')}
        >
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">{dark ? <><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></> : <path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z"/>}</svg>
        </button>
      </header>
      <main>
        {route.page === 'home' && <Home onOpen={(id) => go({ page: 'conference', id, tab: 'materials' })} />}
        {route.page === 'departments' && <Departments />}
        {route.page === 'settings' && <Settings theme={theme} onTheme={setTheme} />}
        {conferenceId && (
          <>
            <div className="conference-heading"><a className="back-link" href="#/" onClick={event => { event.preventDefault(); go({ page: 'home' }); }}>Все конференции</a>
              <h2>{conference?.id === conferenceId ? conference.title ?? 'Без названия' : 'Конференция'}</h2>
              <nav aria-label="Этапы подготовки справки" className="workflow-steps">
                {tabs.map(([value, label], index) => (
                  <button
                    key={value}
                    aria-current={
                      (route.page === 'conference' && route.tab === value)
                      || (route.page === 'talk' && value === 'review') ? 'step' : undefined
                    }
                    onClick={() => go({ page: 'conference', id: conferenceId, tab: value })}
                  >
                    <span className="step-number" aria-hidden="true">{index + 1}</span>{label}
                  </button>
                ))}
              </nav>
            </div>
            {error && <p role="alert">{error}</p>}
          </>
        )}
        {route.page === 'conference' && route.tab === 'materials' && (
          <Materials
            key={route.id}
            conferenceId={route.id}
            onPrepared={() => go({ page: 'conference', id: route.id, tab: 'talks' })}
          />
        )}
        {route.page === 'conference' && route.tab === 'talks' && <Preparation key={route.id} conferenceId={route.id} onConfirmed={() => {}} onStarted={() => go({ page: 'conference', id: route.id, tab: 'review' })} />}
        {route.page === 'conference' && route.tab === 'review' && (
          <Processing
            key={route.id}
            conferenceId={route.id}
            onOpen={(talk) => go({ page: 'talk', id: route.id, talkId: talk.id })}
          />
        )}
        {route.page === 'conference' && route.tab === 'report' && <ReportPanel key={route.id} conferenceId={route.id} />}
        {route.page === 'talk' && (
          <SummaryReview
            key={route.talkId}
            talkId={route.talkId}
            conferenceId={route.id}
            onNavigate={(talkId) => go({ page: 'talk', id: route.id, talkId })}
            onClose={() => go({ page: 'conference', id: route.id, tab: 'review' })}
          />
        )}
      </main>
    </>
  );
}
