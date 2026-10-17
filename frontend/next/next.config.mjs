// 固定同源入口的前端基底路徑，業務 API 仍由瀏覽器直連目前後端。
const nextConfig = { basePath: '/next', output: 'standalone', trailingSlash: true }

export default nextConfig
