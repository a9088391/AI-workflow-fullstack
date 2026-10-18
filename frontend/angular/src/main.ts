import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app/app.config';
import { App } from './app/app';

bootstrapApplication(App, appConfig)
  // 啟動失敗時保留診斷訊息，避免空白頁完全沒有線索。
  .catch((err) => console.error(err));
