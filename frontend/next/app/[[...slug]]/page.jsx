import Site from '../site.jsx'

// 讓已核准的深層網址直接開啟及刷新；未知路徑交由頁面顯示找不到。
export default async function Page({ params, searchParams }) {
  const route = await params
  const query = await searchParams
  const path = `/next/${(route.slug || []).join('/')}`
  return <Site path={path} query={query} />
}
