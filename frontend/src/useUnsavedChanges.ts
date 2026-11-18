import { useCallback, useEffect, useRef } from 'react';

export function useUnsavedChanges(dirty: boolean) {
  const allowed = useRef(false);
  const allowNavigation = useCallback(() => { allowed.current = true; }, []);
  useEffect(() => {
    allowed.current = false;
    function unload(event: BeforeUnloadEvent) { if (dirty && !allowed.current) event.preventDefault(); }
    function navigate(event: Event) {
      if (allowed.current) { allowed.current = false; return; }
      if (dirty && !window.confirm('Есть несохранённые изменения. Покинуть страницу?')) event.preventDefault();
    }
    window.addEventListener('beforeunload', unload);
    window.addEventListener('rapporteur-navigate', navigate);
    return () => {
      window.removeEventListener('beforeunload', unload);
      window.removeEventListener('rapporteur-navigate', navigate);
    };
  }, [dirty]);
  return allowNavigation;
}
