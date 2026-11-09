/** Tailwind 정적 빌드 설정 — Play CDN(cdn.tailwindcss.com) 대체용.
 *  content: 실제 클래스가 등장하는 파일들을 스캔해 사용된 유틸리티만 생성.
 *  UI에 새 Tailwind 클래스를 추가하면 이 빌드를 다시 실행해야 함:
 *    npx tailwindcss@3.4.17 -c tailwind/tailwind.config.cjs -i tailwind/input.css -o static/tailwind.css --minify
 */
module.exports = {
  content: ['./static/index.html', './static/app.js', './static/partials/**/*.html'],
  theme: { extend: {} },
  plugins: [],
};
