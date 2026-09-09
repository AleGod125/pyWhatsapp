import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { SyncService, normalizeSyncStatus } from './sync.service';

describe('SyncService', () => {
  beforeEach(() =>
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }),
  );
  afterEach(() => TestBed.inject(HttpTestingController).verify());

  it('starts a manual reconciliation with POST', () => {
    const service = TestBed.inject(SyncService);
    service.run().subscribe();
    const request = TestBed.inject(HttpTestingController).expectOne(
      'http://localhost:5000/api/v1/sync/run',
    );
    expect(request.request.method).toBe('POST');
    request.flush({ state: 'running' });
  });

  it('normalizes running and completion data from SSE', () => {
    expect(normalizeSyncStatus({ state: 'running' }).state).toBe('running');
    const complete = normalizeSyncStatus({ state: 'complete', messages_new: 57 });
    expect(complete.state).toBe('complete');
    expect(complete.messagesNew).toBe(57);
  });

  it('el adaptador lee el resumen que manda el backend', () => {
    const estado = normalizeSyncStatus({
      state: 'complete',
      summary: {
        chats_total: 40,
        with_cursor: 13,
        waiting_seed: 27,
        retried: 2,
        retry_pending: 1,
        recovered_messages: 0,
        new_seeds: 0,
        drive_pending: 0,
      },
    });

    expect(estado.summary).toEqual({
      chatsTotal: 40,
      withCursor: 13,
      waitingSeed: 27,
      retried: 2,
      retryPending: 1,
      recoveredMessages: 0,
      newSeeds: 0,
      drivePending: 0,
    });
  });

  it('sin resumen no se inventa uno', () => {
    expect(normalizeSyncStatus({ state: 'complete' }).summary).toBeUndefined();
  });

  // --- El modo: qué ciclo es -----------------------------------------------
  //
  // Los dos ponen `state: 'running'` y no se avisan igual. La búsqueda rápida
  // dura segundos; la excavación completa, minutos, y solo esa justifica
  // pedirle al usuario que espere y quitarle el botón de en medio.
  //
  // Y viene del backend a propósito: si el modo viviera solo en el navegador,
  // recargar la página devolvería el botón con la excavación a medias.

  it('el modo llega tal cual desde el backend', () => {
    expect(normalizeSyncStatus({ state: 'running', mode: 'full' }).mode).toBe('full');
    expect(normalizeSyncStatus({ state: 'running', mode: 'incremental' }).mode).toBe(
      'incremental',
    );
  });

  it('un modo que no existe no se acepta como bueno', () => {
    // Inventarse `full` a partir de basura pintaría el cartel largo por nada.
    expect(normalizeSyncStatus({ mode: 'lo-que-sea' }).mode).toBeUndefined();
    expect(normalizeSyncStatus({}).mode).toBeUndefined();
  });

  it('se leen las cifras con las que se cuenta el avance', () => {
    const estado = normalizeSyncStatus({
      state: 'running',
      chats_processed: 12,
      chats_total: 340,
      chats_reopened: 7,
    });

    expect(estado.chatsProcessed).toBe(12);
    expect(estado.chatsTotal).toBe(340);
    expect(estado.chatsReopened).toBe(7);
  });
});
