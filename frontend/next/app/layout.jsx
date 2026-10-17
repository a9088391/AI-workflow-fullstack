import './style.css'

export const metadata = { title: '文章集｜Next.js', description: '會員文章網站' }

// 共用頁面外殼保留繁體中文語意，互動與目前後端在瀏覽器初始化。
export default function RootLayout({ children }) {
  return <html lang="zh-Hant"><body>{children}</body></html>
}
