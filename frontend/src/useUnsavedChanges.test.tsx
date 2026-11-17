import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { useUnsavedChanges } from './useUnsavedChanges';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
test('разрешение не сохраняется, если другой редактор отменил переход', () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  renderHook(() => useUnsavedChanges(true));
  const veto = (event: Event) => event.preventDefault();
  window.addEventListener('rapporteur-navigate', veto);
  try {
    window.dispatchEvent(new Event('rapporteur-navigate', { cancelable: true }));
    window.dispatchEvent(new Event('rapporteur-navigate', { cancelable: true }));
    expect(confirm).toHaveBeenCalledTimes(2);
  } finally { window.removeEventListener('rapporteur-navigate', veto); }
});
