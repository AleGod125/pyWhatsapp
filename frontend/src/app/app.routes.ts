import { Routes } from '@angular/router';
import {
  connectGoogleGuard,
  dashboardGuard,
  invitadoGuard,
  pairingGuard,
} from './core/guards/onboarding.guard';

/**
 * El orden de la ruta NO decide nada: cada guard pregunta al backend por
 * `/onboarding/status`. Escribir `/dashboard` a mano no salta ningún paso.
 *
 * LA PORTADA VA PRIMERO, Y NO ES UN ADORNO
 * ----------------------------------------
 * `''` es la portada, y a ella va también todo lo que no encaje en ninguna
 * ruta. Quien todavía no ha entrado y escribe `/dashboard` acaba ahí —lo
 * decide `RUTA_DE_PASO.login` en el guard— en vez de en un formulario que no
 * explica qué es esto ni por qué debería confiarle su WhatsApp.
 *
 * `/login` y `/register` siguen siendo direcciones normales: la portada lleva
 * a ellas, y quien tenga el enlace guardado entra directo. Obligar a pasar por
 * la portada al que ya sabe lo que quiere sería estorbar, no presentar.
 */
export const routes: Routes = [
  {
    // LA PORTADA ES LA PUERTA. Sin guard: es lo único que tiene que poder ver
    // cualquiera, haya entrado o no, y aunque el backend no responda.
    path: '',
    pathMatch: 'full',
    loadComponent: () =>
      import('./features/landing/landing-page.component').then(
        (m) => m.LandingPageComponent,
      ),
  },
  {
    path: 'login',
    canActivate: [invitadoGuard],
    loadComponent: () =>
      import('./features/auth/login-page.component').then((m) => m.LoginPageComponent),
  },
  {
    path: 'register',
    canActivate: [invitadoGuard],
    loadComponent: () =>
      import('./features/auth/register-page.component').then((m) => m.RegisterPageComponent),
  },
  {
    path: 'connect-google',
    canActivate: [connectGoogleGuard],
    loadComponent: () =>
      import('./features/auth/connect-google-page.component').then(
        (m) => m.ConnectGooglePageComponent,
      ),
  },
  {
    // Vuelta del OAuth. Sin guard a propósito: es quien AVERIGUA el estado
    // recién cambiado, así que no puede exigir conocerlo de antemano.
    path: 'auth/google/callback',
    loadComponent: () =>
      import('./features/auth/google-callback-page.component').then(
        (m) => m.GoogleCallbackPageComponent,
      ),
  },
  {
    path: 'pairing',
    canActivate: [pairingGuard],
    loadComponent: () =>
      import('./features/pairing/pairing-page.component').then((m) => m.PairingPageComponent),
  },
  {
    path: 'dashboard',
    canActivate: [dashboardGuard],
    loadComponent: () =>
      import('./features/dashboard/dashboard-page.component').then((m) => m.DashboardPageComponent),
  },
  {
    path: 'dashboard/:chatId',
    canActivate: [dashboardGuard],
    loadComponent: () =>
      import('./features/dashboard/dashboard-page.component').then((m) => m.DashboardPageComponent),
  },
  // Una URL que no existe lleva a la portada, no al panel: mandar a alguien
  // sin sesión directamente al panel solo consigue que el guard lo rebote al
  // acceso, y ahí no hay nada que le explique dónde ha llegado.
  { path: '**', redirectTo: '' },
];
