# StudyMate AI Pro — Cập nhật (Giao diện kiểu ChatGPT/Claude + Tài khoản)

## Những gì vừa thêm so với bản trước

### 1. Hệ thống tài khoản (đăng ký / đăng nhập / đăng xuất)
- Trang **`/register`** và **`/login`** riêng, giao diện đồng bộ với thương hiệu StudyMate.
- Mật khẩu được băm bằng `werkzeug.security` (PBKDF2), **không lưu plaintext**.
- Đăng nhập dùng session cookie (HttpOnly, SameSite=Lax) — Flask ký cookie bằng `SECRET_KEY`.
- Toàn bộ trang chat (`/`) và các API (`/api/chat`, `/api/upload`, `/api/conversations*`) đều yêu cầu đăng nhập; gọi API mà chưa đăng nhập sẽ nhận `401` (frontend tự chuyển hướng về `/login`).
- Dữ liệu tài khoản lưu trong **SQLite** (`studymate.db`, tự tạo cạnh `app.py`, không cần cài thêm gì).

⚠️ Lưu ý quan trọng: thêm `SECRET_KEY=<chuỗi_ngẫu_nhiên_dài>` vào file `.env`. Nếu không đặt, mỗi lần restart server người dùng sẽ bị đăng xuất hết (vì khóa ký session bị sinh lại ngẫu nhiên) — server sẽ tự in ra một gợi ý khi khởi động.

### 2. Lịch sử trò chuyện theo tài khoản (giống ChatGPT/Claude)
- Mỗi tin nhắn được lưu vào bảng `conversations` / `messages` trong SQLite, gắn với `user_id`.
- Sidebar bên trái liệt kê các đoạn chat cũ, bấm vào để mở lại toàn bộ hội thoại.
- Nút **"Đoạn chat mới"** để bắt đầu hội thoại mới; icon thùng rác để xoá đoạn chat (có xác nhận trước khi xoá).
- Đoạn chat mới được tạo tự động ngay khi gửi tin nhắn đầu tiên (tiêu đề lấy từ nội dung câu hỏi đầu tiên).

### 3. Giao diện được thiết kế lại theo phong cách các AI phổ biến (ChatGPT/Claude)
- Bố cục 2 cột: **sidebar** (lịch sử chat + tài khoản) và **khung chat chính**, thay cho layout 4 cột kiểu dashboard cũ.
- Tin nhắn của học sinh hiển thị dạng bong bóng bên phải; câu trả lời AI hiển thị dạng văn bản trơn kèm avatar robot bên trái (không còn bong bóng gradient nặng nề) — giống cách ChatGPT/Claude trình bày.
- Môn học và Chế độ học tập chuyển thành 2 ô chọn dạng "pill" gọn gàng ở thanh trên cùng, thay vì chiếm hẳn một cột lớn.
- Ô nhập câu hỏi dạng thanh bo tròn nổi ở dưới cùng (giống thanh composer của ChatGPT/Claude), có nút đính kèm, nút micro và nút gửi hình mũi tên.
- Sidebar tự thu gọn trên di động (menu hamburger), có overlay khi mở.
- Khu vực tài khoản ở cuối sidebar: avatar chữ cái đầu + tên đăng nhập + menu "Đăng xuất".
- Vẫn giữ nguyên toàn bộ tính năng cũ: streaming trả lời theo thời gian thực, đọc PDF/Word/txt/csv, đọc ảnh, kéo-thả file, dark/light mode, trợ lý giọng nói.

### 4. Tài khoản Developer + Trang thống kê sử dụng (mới)
- Bảng `users` có thêm cột **`role`** (`user` mặc định, hoặc `developer`).
- Khi khởi động lần đầu, server **tự tạo 1 tài khoản developer**:
  - Tên đăng nhập mặc định: `developer` (đổi bằng biến `DEVELOPER_USERNAME` trong `.env`).
  - Nếu chưa đặt `DEVELOPER_PASSWORD` trong `.env`, server tự sinh mật khẩu ngẫu nhiên và **in ra console đúng 1 lần** khi khởi động — hãy đăng nhập và đổi mật khẩu ngay (đổi bằng cách xoá dòng tương ứng trong bảng `users` rồi tạo lại, hoặc tự thêm chức năng đổi mật khẩu sau).
  - Nếu database cũ đã có sẵn tài khoản tên `developer`, server sẽ **tự nâng quyền** tài khoản đó thành `developer` (không tạo trùng).
- Đăng nhập bằng tài khoản này sẽ thấy mục **"Thống kê (Developer)"** trong menu tài khoản ở sidebar (👤 → góc dưới sidebar), dẫn tới trang **`/developer`**.
- Trang `/developer` hiển thị:
  - Tổng số tài khoản, tổng lượt hỏi AI, lượt hỏi hôm nay / 7 ngày qua, tỉ lệ lỗi.
  - Biểu đồ cột số lượt sử dụng theo từng ngày (14 ngày gần nhất).
  - Phân bổ lượt hỏi theo **môn học** và theo **chế độ học tập**.
  - Danh sách người dùng hoạt động nhiều nhất + toàn bộ danh sách tài khoản.
  - **Không lưu/hiển thị nội dung câu hỏi hay câu trả lời** — chỉ số liệu tổng hợp (độ dài, môn học, chế độ, trạng thái thành công/lỗi), lưu trong bảng mới `usage_logs`.
- Route `/developer` được bảo vệ bởi decorator `developer_required`: người dùng thường (`role = user`) sẽ bị chuyển hướng về trang chat; chưa đăng nhập sẽ bị chuyển tới `/login`.

⚠️ Lưu ý: đây là cơ chế "role" đơn giản (một cột trong SQLite), phù hợp cho dự án cá nhân/lớp học. Nếu deploy công khai, nên bổ sung: giới hạn số lần thử đăng nhập (rate limit), trang đổi mật khẩu, và log audit khi có người truy cập `/developer`.

## Cài đặt & chạy

```bash
pip install -r requirements.txt
```

Bản này thêm thư viện `Authlib` (cho đăng nhập Google) và `gunicorn` (để chạy production) vào `requirements.txt`.

Tạo file `.env` cùng thư mục:

```
XAI_API_KEY=xai-xxxxxxxxxxxxxxxx
CONSOLEX_API_BASE=https://api.x.ai/v1
CONSOLEX_MODEL=grok-4.5
SECRET_KEY=mot-chuoi-ngau-nhien-that-dai-va-kho-doan
```

Muốn bật thanh toán thật cho Premium/Max, xem thêm biến `.env` cho VNPAY/VietQR ở mục 14.

Chạy:

```bash
python app.py
```

Truy cập `http://localhost:5000` → sẽ tự chuyển tới `http://localhost:5000/login` nếu chưa đăng nhập. Bấm "Đăng ký ngay" để tạo tài khoản đầu tiên.

## 5. Đăng nhập bằng Google (mới)

- Trang `/login` và `/register` giờ có giao diện mới (nền gradient, glassmorphism) và **tự động hiện nút "Đăng nhập với Google"** nếu bạn đã cấu hình Client ID/Secret trong `.env`. Chưa cấu hình thì nút tự ẩn, app vẫn chạy bình thường với tên đăng nhập/mật khẩu như cũ.
- Tài khoản tạo qua Google được lưu trong cùng bảng `users`, cột `password_hash` để trống — **nghĩa là server không bao giờ nắm giữ, xem, hay lưu mật khẩu Google thật của người dùng**; toàn bộ việc xác thực diễn ra ở phía Google, app chỉ nhận lại `id`, `email`, `tên hiển thị` sau khi người dùng đồng ý.
- Nếu người dùng đăng nhập bằng cùng email đã có tài khoản mật khẩu trước đó, tài khoản đó sẽ được **liên kết thêm** OAuth thay vì tạo trùng.

### Lấy Google Client ID / Secret
1. Vào [Google Cloud Console](https://console.cloud.google.com/) → tạo project mới (hoặc chọn project có sẵn).
2. Vào **APIs & Services → OAuth consent screen** → chọn "External" → điền tên app, email → lưu.
3. Vào **APIs & Services → Credentials → Create Credentials → OAuth client ID** → chọn "Web application".
4. Ở mục **Authorized redirect URIs**, thêm:
   - `http://localhost:5000/auth/google/callback` (để test local)
   - `https://tenmiencuaban.com/auth/google/callback` (khi deploy thật — thay bằng domain thật của bạn)
5. Copy `Client ID` và `Client secret`, dán vào `.env`:
   ```
   GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=xxxxxxxx
   ```

(Muốn thêm Apple Sign In sau này: quy trình tương tự nhưng cần tài khoản Apple Developer trả phí 99 USD/năm và cấu hình phức tạp hơn — cho mình biết nếu bạn muốn làm tiếp phần này.)

## Lưu ý bảo mật (đã cân nhắc nhưng không phóng đại)
- Mật khẩu băm bằng PBKDF2 (`werkzeug.security`), không hard-code, không lưu plaintext.
- Session cookie có `HttpOnly` + `SameSite=Lax`.
- Input validation cơ bản cho form đăng ký (độ dài tên đăng nhập, độ dài mật khẩu tối thiểu, kiểm tra khớp mật khẩu nhập lại).
- API key vẫn chỉ đọc từ biến môi trường.
- Giới hạn upload: `MAX_CONTENT_LENGTH = 15MB` tổng, `6MB` riêng cho ảnh.
- **Chưa có**: rate limiting cho đăng nhập/đăng ký (nên thêm `flask-limiter` nếu deploy public để chống brute-force), CSRF token riêng cho form (hiện dựa vào `SameSite=Lax` + same-origin), xác thực email, quên mật khẩu. Đây là những phần nên bổ sung trước khi đưa lên môi trường thật với nhiều người dùng.

## 6. Public app lên thành website chính thức (Deploy)

App hiện chạy bằng `app.run(...)` — chỉ phù hợp để **test trên máy cá nhân**, không nên dùng khi có người dùng thật. Dưới đây là 2 cách phổ biến để đưa app lên mạng.

### Cách A — Nhanh nhất: Render.com (miễn phí, khuyên dùng cho dự án cá nhân/lớp học)

1. Đưa toàn bộ code (`app.py`, `requirements.txt`, ...) lên một repo GitHub.
2. Vào [render.com](https://render.com) → đăng nhập bằng GitHub → **New → Web Service** → chọn repo vừa tạo.
3. Cấu hình:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Vào tab **Environment**, thêm toàn bộ biến trong `.env` của bạn (XAI_API_KEY, SECRET_KEY, GOOGLE_CLIENT_ID, ...) — **không commit file `.env` lên GitHub**.
5. Bấm **Deploy**. Render sẽ cấp cho bạn 1 domain dạng `https://ten-app.onrender.com` kèm HTTPS miễn phí sẵn.
6. Quay lại Google Cloud Console, thêm `https://ten-app.onrender.com/auth/google/callback` vào danh sách Redirect URI (bước ở mục 5 phía trên).
7. (Tuỳ chọn) Gắn domain riêng của bạn trong tab **Settings → Custom Domain** của Render.

⚠️ Lưu ý: gói miễn phí của Render dùng ổ đĩa tạm — file `studymate.db` (SQLite) **có thể bị mất khi server khởi động lại**. Với dự án thật có nhiều người dùng, nên: (a) nâng cấp gói có "Persistent Disk", hoặc (b) chuyển sang PostgreSQL (Render có sẵn dịch vụ Postgres miễn phí, cần sửa lại phần kết nối DB trong `app.py`).

### Cách B — Tự chủ hơn: VPS riêng (DigitalOcean, Vultr, AWS Lightsail...)

1. Thuê 1 VPS Ubuntu (rẻ nhất khoảng 4-6 USD/tháng), trỏ domain của bạn về IP của VPS (bản ghi A).
2. SSH vào VPS, cài Python, clone code, tạo virtualenv, `pip install -r requirements.txt`, tạo file `.env`.
3. Chạy app bằng gunicorn làm service nền (systemd), ví dụ file `/etc/systemd/system/studymate.service`:
   ```
   [Unit]
   Description=StudyMate AI Pro
   After=network.target

   [Service]
   WorkingDirectory=/duong/dan/toi/studymate
   ExecStart=/duong/dan/toi/studymate/venv/bin/gunicorn app:app --bind 127.0.0.1:8000 --workers 3
   Restart=always
   EnvironmentFile=/duong/dan/toi/studymate/.env

   [Install]
   WantedBy=multi-user.target
   ```
   Sau đó: `sudo systemctl enable --now studymate`.
4. Cài Nginx làm reverse proxy (chuyển tiếp từ cổng 80/443 vào 127.0.0.1:8000), rồi cài `certbot` để lấy chứng chỉ HTTPS miễn phí (Let's Encrypt): `sudo certbot --nginx -d tenmiencuaban.com`.
5. Cập nhật Redirect URI ở Google thành `https://tenmiencuaban.com/auth/google/callback` như ở Cách A bước 6.

Cách B tốn công cấu hình hơn nhưng bạn toàn quyền kiểm soát dữ liệu (file SQLite không bị mất khi restart) và không giới hạn tài nguyên như gói miễn phí.

## 7. Sửa lỗi hiển thị (mới)
- **Chữ tiếng Việt bị vỡ ký tự khi AI trả lời**: nguyên nhân do thư viện `requests` tự đoán sai encoding của response streaming từ xAI (mặc định ISO-8859-1 thay vì UTF-8). Đã ép `resp.encoding = 'utf-8'` trong `stream_consolex_ai()` — lỗi này đã hết.
- **Công thức toán hiện ra dạng chữ thô** (`$$...$$`, `\begin{cases}`...): app trước đó chưa có bộ render LaTeX. Đã thêm **KaTeX** (qua CDN) để tự động render công thức đẹp, và dặn AI trong system prompt luôn dùng đúng cú pháp `$$...$$` / `\(...\)`.
- **Chữ tràn ra ngoài khung chat**: đã thêm `overflow-wrap: anywhere` cho phần nội dung AI trả lời để tự xuống dòng với chuỗi dài không có khoảng trắng.
- **Đã bỏ đăng nhập Facebook** khỏi toàn bộ app (route, nút bấm, biến môi trường) — chỉ còn đăng nhập bằng Google + tên đăng nhập/mật khẩu.

## 8. Dự án, Ghim, Tìm kiếm, Cài đặt cá nhân (mới)

Sidebar được nâng cấp thêm cấu trúc kiểu ChatGPT/Claude:

- **Ô tìm kiếm** phía trên sidebar: lọc đoạn chat theo tiêu đề ngay khi gõ (client-side, không cần reload).
- **Dự án**: bấm dấu "+" cạnh mục "Dự án" để tạo một dự án mới (vd: "Ôn thi HK1"), sau đó dùng menu "⋯" trên từng đoạn chat để **chuyển đoạn chat vào dự án**. Bấm vào tên dự án trong sidebar để lọc riêng các đoạn chat thuộc dự án đó.
- **Ghim đoạn chat**: menu "⋯" trên từng đoạn chat có mục Ghim/Bỏ ghim — các đoạn đã ghim nổi lên mục "Đã ghim" riêng, luôn ở trên cùng.
- **Đổi tên đoạn chat**: cũng nằm trong menu "⋯".
- **Menu tài khoản** (góc dưới sidebar) có thêm 3 mục mới:
  - **Cài đặt**: chọn giao diện Sáng/Tối/Theo hệ thống, ngôn ngữ (Tiếng Việt/English — dịch nhẹ phần khung giao diện, không dịch nội dung AI trả lời), môn học & chế độ mặc định khi mở đoạn chat mới, và nút **xoá toàn bộ lịch sử** (có xác nhận trước khi xoá).
  - **Trợ giúp & phím tắt**: liệt kê các phím tắt — `Ctrl/Cmd+K` đoạn chat mới, `Ctrl/Cmd+/` mở trợ giúp, `Esc` đóng hộp thoại.
  - **Nâng cấp gói**: chỉ là bản xem trước giao diện, **chưa có chức năng thanh toán thật** — ghi rõ trong hộp thoại để tránh gây hiểu lầm.
- Tất cả tuỳ chọn ở trên được lưu theo tài khoản (cột `preferences` mới trong bảng `users`, dạng JSON) — đăng nhập ở máy khác vẫn giữ nguyên cài đặt.
- **Banner thông báo hệ thống**: nếu developer bật banner (xem mục 9), mọi người dùng đã đăng nhập sẽ thấy dòng thông báo trong sidebar, có thể bấm "x" để tạm ẩn (ẩn theo phiên trình duyệt, hiện lại nếu mở tab mới hoặc banner được đổi nội dung).

## 9. Công cụ quản lý mới cho tài khoản Developer

Trang `/developer` có thêm mục **"Quản lý hệ thống"**:

- **Banner thông báo**: ô nhập nội dung (tối đa 300 ký tự) + công tắc bật/tắt, bấm "Lưu banner" để áp dụng ngay cho toàn bộ người dùng.
- **Công tắc đăng nhập Google (runtime)**: chỉ hiện nếu `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` đã có trong `.env`. Cho phép Bật/Tắt/Về mặc định nút "Đăng nhập với Google" **ngay lập tức, không cần sửa `.env` hay khởi động lại server** — hữu ích khi cần tạm khoá đăng nhập Google (vd: đang debug OAuth) mà không ảnh hưởng đăng nhập bằng mật khẩu.
- **Xuất CSV**: tải toàn bộ `usage_logs` (không có nội dung câu hỏi/trả lời, chỉ số liệu) ra file `usage_logs.csv` để phân tích thêm bằng Excel/Google Sheets.
- **Tìm kiếm tài khoản**: ô tìm theo tên đăng nhập phía trên bảng "Toàn bộ tài khoản".
- **Thăng/hạ quyền developer trực tiếp từ bảng tài khoản**: nút "Nâng lên developer" / "Hạ xuống user" trên từng dòng. Có 2 lớp bảo vệ:
  - Không thể tự hạ quyền chính tài khoản đang đăng nhập.
  - Không thể hạ quyền nếu đó là **developer cuối cùng** của hệ thống — luôn phải còn ít nhất 1 tài khoản developer.

⚠️ Không cần cài thêm thư viện nào cho các tính năng ở mục 8–9 — toàn bộ dùng lại `flask`, `sqlite3`, `csv` (thư viện chuẩn của Python), không cập nhật `requirements.txt`.

## 10. Gói sử dụng: Free / Premium / Max (mới)

> ⚠️ Lưu ý: bản `app.py` bạn gửi lần này là một nhánh phát triển khác với các bản trước (đã có sẵn hệ thống vai trò `user → developer → admin → super_admin`, AI Tutor tuỳ chỉnh, nhật ký audit, v.v. — xem mục 13). Mục 10–11 dưới đây là phần mình vừa hoàn thiện theo đúng yêu cầu mới nhất của bạn (mục 10 nay đã cập nhật để khớp với cổng thanh toán thật ở mục 14). "Báo lỗi câu trả lời", "Bộ nhớ AI" và điểm thưởng/chuỗi ngày học (gamification) **đã có sẵn** trong nhánh này — xem mục 16.

Đã bỏ hẳn chữ **"Pro"** khỏi mọi nơi hiển thị cho tài khoản thường — tên app giờ đổi động theo gói:

| Gói | Ai có | Giới hạn đọc file/ảnh mỗi 24h | Dung lượng tối đa/file | Tên app hiển thị |
|---|---|---|---|---|
| 🆓 **Free** | Mặc định mọi tài khoản mới | 20 lượt | 20MB | `StudyMate AI` |
| 💎 **Premium** | Admin gán tay từ `/developer` | 50 lượt | 500MB | `StudyMate AI Premium` |
| 🚀 **Max** | Admin gán tay, **hoặc tự động nếu vai trò ≥ Developer** | Không giới hạn | 1GB | `StudyMate AI Max` |

- Cột `plan` mới trong bảng `users` (mặc định `'free'`) lưu gói do Admin gán thủ công — **nhưng** tài khoản có vai trò `developer`/`admin`/`super_admin` luôn được tính là **Max vô điều kiện** ngay cả khi cột `plan` vẫn ghi `'free'` (hàm `effective_plan()` ưu tiên vai trò trước, không cần Admin phải gán tay cho từng dev). Vì vậy trang `/developer` **không cho đổi gói** với các tài khoản từ Developer trở lên (nút bị ẩn, kèm chú thích lý do).
- Giới hạn *số lượt* đọc file/ảnh tính theo **cửa sổ trượt 24 giờ** (không phải theo lịch nửa đêm reset) — bảng mới `file_uploads` ghi lại mỗi lượt tải lên, cứ quá 24h thì lượt đó "hết hạn" và tự nhường chỗ cho lượt mới. Giới hạn dung lượng còn siết luôn cả bước trích chữ từ PDF/Word: Free cắt ở ~12.000 ký tự, Premium ~48.000 ký tự, Max không cắt.
- **Đổi gói (Admin trở lên)**: bảng "Toàn bộ tài khoản" ở `/developer` có thêm cột **Gói** + dropdown đổi gói tại chỗ (`POST /developer/users/<id>/plan`), có ghi audit log. Đổi sang Free thì xoá hạn dùng ngay; đổi sang Premium/Max thì **chỉ tặng đúng 1 tháng miễn phí** (dùng chung hàm `grant_plan_upgrade()` với cổng thanh toán thật ở mục 14, không phải gán vĩnh viễn) — hết hạn tự rơi về Free như một lượt nâng cấp bình thường.
- **Xem gói + hạn mức của chính mình**: `GET /api/plan` trả về gói hiện tại, hạn dùng còn lại (nếu là gói trả phí), đã dùng bao nhiêu/bao nhiêu lượt hôm nay, có phải "miễn phí theo vai trò" không, và có đang được hưởng ưu đãi lần đầu không. Hiển thị ngay trong **Cài đặt** (thanh tiến trình nhỏ, tự làm mới sau mỗi lần tải file).
- **Hộp thoại "Nâng cấp gói"**: hiện đúng 3 cột Free/Premium/Max với số liệu thật, tự khoanh viền cột gói hiện tại của người xem, liệt kê các Chế độ suy nghĩ mở khoá ở từng gói (xem mục 11), và **giờ có cổng thanh toán thật** — xem chi tiết ở mục 14.

## 11. Chế độ suy nghĩ của AI: Trợ Lý / Học Giả / Giáo Sư / Thiên Tài (mới)

Một dropdown mới ngay cạnh 2 ô "Môn học" / "Chế độ" ở thanh trên cùng, cho học sinh chọn AI nên "đầu tư" bao nhiêu công sức suy luận cho câu trả lời:

| Chế độ | Icon | Gói tối thiểu | Ngân sách token | Ý tưởng |
|---|---|---|---|---|
| Trợ Lý | 💬 | Free | 800 | Mặc định, nhanh, cân bằng |
| Học Giả | 📖 | Premium | 1.400 | Suy luận từng bước kỹ hơn trước khi chốt đáp án |
| Giáo Sư | 🎓 | Premium | 1.600 | Giải thích mở rộng — nhiều ví dụ, liên hệ thực tế |
| Thiên Tài | 🌟 | **Max** (độc quyền) | 2.200 | Kết hợp cả suy luận sâu lẫn giải thích mở rộng — mạnh nhất |

- Học Giả + Giáo Sư tương ứng đúng 2 chế độ "deepthinking"/"extra" bạn yêu cầu (mở khoá từ Premium); Thiên Tài là chế độ **độc quyền Max** duy nhất, kết hợp cả hai — bạn có thể đổi tên hiển thị bất cứ lúc nào ở dict `THINKING_MODES` trong `app.py`, không cần sửa logic.
- Trong dropdown, chế độ chưa mở khoá vẫn hiện đầy đủ (kèm mô tả) nhưng có khoá 🔒 + nhãn gói cần có — bấm vào sẽ mở thẳng hộp thoại "Nâng cấp gói" thay vì chọn được.
- **Chặn ở cả server, không chỉ ẩn ở giao diện**: kể cả khi ai đó tự gọi thẳng `POST /api/chat` với `thinkingMode: "genius"` mà tài khoản đang là Free, server vẫn tự động hạ về `"standard"` (hàm `resolve_thinking_mode()`) — không tin tưởng dữ liệu phía client gửi lên.
- Chế độ đang chọn được gắn thêm 1 đoạn hướng dẫn (`prompt_hint`) vào system prompt và đổi luôn `max_tokens` gửi cho model — không tốn thêm lượt gọi API nào ngoài 1 lượt chat bình thường.

⚠️ Chưa lưu chế độ suy nghĩ đang chọn vào tuỳ chọn cá nhân (`preferences`) — mỗi lần tải lại trang sẽ về lại "Trợ Lý" mặc định. Muốn nhớ lựa chọn qua các lần đăng nhập thì cần thêm 1 field vào `DEFAULT_PREFERENCES`/`get_preferences()`/`set_preferences()` — có thể làm tiếp nếu bạn cần.

## 12. Sửa 1 lỗi ẩn khi kết hợp streaming + SQLite (mới, quan trọng)

Trong lúc kiểm thử tính năng ở mục 10–11, phát hiện một lỗi có sẵn từ trước (không liên quan tới gói/chế độ suy nghĩ, nhưng ảnh hưởng tới **mọi** câu trả lời AI): các lượt chat thật ra bị lỗi ngầm **"Cannot operate on a closed database"** ngay khi AI bắt đầu trả lời.

**Nguyên nhân:** `/api/chat` dùng `stream_with_context()` để giữ `request`/`session`/`g` sống trong lúc trả lời dạng streaming (SSE) — nhưng cơ chế này **không** ngăn được `teardown_appcontext` (hàm đóng kết nối SQLite `g._database`) chạy sớm hơn generator thật sự bắt đầu. Kết quả: bất kỳ chỗ nào trong generator (hoặc trong các hàm nó gọi tới, kể cả gián tiếp — ví dụ `stream_consolex_ai()` đọc cấu hình model/temperature qua `get_setting()`) mà dùng lại `get_db()`/`g` đều đụng phải kết nối SQLite **đã bị đóng từ trước**.

**Đã sửa:** thêm hàm `open_write_db()` — mở 1 kết nối SQLite **độc lập, không qua `g`**, tự đóng ngay sau khi dùng xong. Áp dụng cho mọi thao tác ghi DB xảy ra **bên trong** generator streaming: lưu câu trả lời của AI vào lịch sử chat, ghi `usage_logs` (`log_usage()`), và đọc cấu hình runtime (`get_setting()`/`set_setting()`, vì `stream_consolex_ai()` gọi tới 2 hàm này để lấy model/temperature ghi đè). Đã kiểm thử lại toàn bộ luồng chat (thành công lẫn báo lỗi) — không còn gặp lỗi này nữa.

## 13. Vai trò & công cụ quản trị đã có sẵn trong nhánh này (ghi chú lại, không phải mình làm)

Để tránh trùng lặp tài liệu, đây là danh sách nhanh những gì `app.py` bạn gửi **đã có sẵn** trước khi mình động vào (mình chỉ dùng/nối thêm vào, không viết lại): hệ thống vai trò 4 cấp `user → developer → admin → super_admin` (`ROLE_ORDER`/`role_rank()`), khoá/mở khoá tài khoản, reset session, xoá tài khoản, AI Tutor tuỳ chỉnh (`/api/tutors`), API key cá nhân (`/api/keys`, `/api/v1/ping`), Playground thử prompt cho Developer trở lên, ghi đè model/temperature/system-prompt chung không cần restart, chế độ bảo trì, và nhật ký audit (`/developer/audit`, chỉ Super Admin xem được). Nếu cần tài liệu chi tiết cho từng phần này, cho mình biết để viết bổ sung.

## 14. Nâng cấp gói: thanh toán thật theo tháng + ưu đãi lần đầu (mới)

Gói Premium/Max giờ là **thuê bao theo THÁNG** (không còn "gán vĩnh viễn"), có 2 cách thanh toán:

| Gói | Giá gốc | Ưu đãi lần đầu |
|---|---|---|
| 💎 Premium | 30.000đ/tháng | **50%** cho 3 tháng đầu → 15.000đ/tháng |
| 🚀 Max | 50.000đ/tháng | **50%** cho 3 tháng đầu → 25.000đ/tháng |

- **Ưu đãi lần đầu**: đếm theo tổng số đơn **đã thanh toán thành công** trong lịch sử tài khoản (`payment_orders.status = 'paid'`), không phân biệt Premium hay Max — đủ 3 đơn thì từ đơn thứ 4 trở đi tính giá bình thường, không cần làm gì thêm. Chỉnh mức % hoặc số tháng ưu đãi ở 2 hằng số `FIRST_TIME_DISCOUNT_PCT` / `FIRST_TIME_DISCOUNT_MONTHS` đầu file `app.py`.
- **Hết hạn tự rơi về Free**: mỗi lượt thanh toán chỉ cấp đúng 1 tháng (cột `plan_expires_at` mới trong bảng `users`), tính lại từ **thời điểm thanh toán** (không cộng dồn nếu gia hạn sớm). Hết hạn mà chưa thanh toán tiếp thì `effective_plan()` tự trả về Free ngay lần tải trang kế tiếp — không cần cron job/background task nào.
- **2 phương thức thanh toán**, cấu hình qua `.env` (thiếu biến nào thì phương thức đó tự ẩn khỏi giao diện, không lỗi):
  ```
  # VNPAY (thẻ ATM nội địa/Visa/Mastercard/JCB) — đăng ký merchant tại https://vnpay.vn
  VNPAY_TMN_CODE=xxxxxxxx
  VNPAY_HASH_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  VNPAY_PAYMENT_URL=https://sandbox.vnpayment.vn/paymentv2/vpcpay.html   # đổi sang URL production khi go-live

  # Chuyển khoản VietQR (quét mã QR bằng app ngân hàng/MoMo/ZaloPay) — không cần đăng ký merchant
  VIETQR_BANK_ID=mbbank          # tên ngân hàng hoặc mã BIN, xem danh sách tại vietqr.io
  VIETQR_ACCOUNT_NO=xxxxxxxxxxxx
  VIETQR_ACCOUNT_NAME=NGUYEN VAN A
  ```
  - VNPAY chốt đơn **tự động** qua IPN (`vnpay_ipn()`), có xác thực chữ ký HMAC-SHA512 — không tin bất kỳ tham số nào từ trình duyệt gửi lên.
  - Chuyển khoản VietQR chốt đơn **thủ công**: Admin vào `/developer` bấm "Xác nhận đã nhận tiền" sau khi kiểm tra sao kê (app không có quyền đọc sao kê ngân hàng tự động).
- **Admin "tặng" gói cho học sinh**: vẫn thao tác y hệt mục 10 (đổi gói trực tiếp từ `/developer`), nhưng giờ dùng chung logic với cổng thanh toán thật nên **chỉ tặng đúng 1 tháng miễn phí**, không phải vĩnh viễn — hết tháng đó học sinh cần tự thanh toán tiếp (hoặc Admin tặng lại) như mọi tài khoản khác. Không áp dụng cho tài khoản Developer trở lên vì các vai trò đó đã luôn có Max vô điều kiện.
- Hộp thoại "Nâng cấp gói" tự hiện huy hiệu "🎁 Giảm 50% — còn N tháng ưu đãi" + giá gốc gạch ngang khi tài khoản còn đủ điều kiện; hết ưu đãi thì tự quay lại hiện giá gốc, không cần Admin can thiệp.

⚠️ Chưa có: gia hạn tự động trừ tiền định kỳ (app không lưu thông tin thẻ để làm việc đó — học sinh cần tự vào lại nâng cấp mỗi tháng), hoá đơn/biên lai điện tử, hoàn tiền.

## 15. Sửa lỗi giao diện (mới)

- **Nút "Báo lỗi" dưới câu trả lời AI bị vô hình**: nút vẫn nằm trong DOM và bấm được, nhưng CSS `.ai-msg-group:hover .msg-actions` yêu cầu nút phải là **con** của khung tin nhắn để hiện khi rê chuột — trong khi JS lại chèn nút bằng `wrapper.after(bar)`, tức là **anh em cùng cấp**, không phải con, nên `opacity` luôn bằng 0. Đã đổi sang bộ chọn anh em `.ai-msg-group:hover ~ .msg-actions` + thêm `:hover`/`:focus-within` riêng cho nút để cả di động (không có hover) và bàn phím đều bấm được.
- **Nút chuyển giao diện Sáng/Tối không đổi gì cả**: Tailwind được nhúng qua CDN (`cdn.tailwindcss.com`), mặc định chế độ tối theo `prefers-color-scheme` của hệ điều hành (`darkMode: 'media'`) — nghĩa là JS tự bật/tắt class `dark` trên thẻ `<html>` **không có tác dụng gì** với các lớp `dark:` nếu không khai báo lại. Đã thêm `<script>tailwind.config = { darkMode: 'class' }</script>` ngay sau mỗi lần nhúng CDN Tailwind (6 trang HTML trong `app.py`) để JS điều khiển được thật.
- **Avatar robot có hiệu ứng "đang suy nghĩ"**: một dải sáng quét dọc từ **dưới lên trên**, lặp lại — chạy nhanh & rõ trên avatar khi AI đang trả lời, chạy chậm & mờ liên tục ở logo robot trong sidebar để làm avatar chung của cả web.
- **Lỗi "Cannot operate on a closed database" do tự restart giữa lúc đang trả lời**: nguyên nhân khác với lỗi đã sửa ở mục 12 (đó là do `teardown_appcontext` đóng kết nối sớm; lỗi này là do Werkzeug **tự restart cả tiến trình**). Khi chạy bằng `python app.py` với `debug=True`, Werkzeug mặc định bật `use_reloader` — theo dõi thư mục dự án, hễ có file thay đổi là tự khởi động lại server. Vì `studymate.db` (SQLite) bị ghi mỗi khi có tin nhắn mới, nó cũng bị tính là "file thay đổi" → server tự restart ngay giữa lúc đang stream câu trả lời, kết nối SQLite của request đó bị đóng đột ngột. Đã thêm `use_reloader=False` vào `app.run(...)` ở cuối file để tắt cơ chế tự restart này (vẫn giữ `debug=True` để còn thấy traceback khi phát triển) — sửa xong code thì tự dừng (`Ctrl+C`) và chạy lại thủ công.

## 16. Báo lỗi câu trả lời, Bộ nhớ AI, Điểm thưởng & Chuỗi ngày học (đã có sẵn trong nhánh này)

- **Báo lỗi câu trả lời**: mỗi câu trả lời của AI có nút "Báo lỗi" (hiện khi rê chuột/chạm vào tin nhắn — xem lỗi hiển thị đã sửa ở mục 15), mở hộp thoại cho học sinh chọn lý do + ghi chú thêm, lưu vào `/developer` để Admin xem và đánh dấu đã xử lý.
- **Bộ nhớ AI**: AI tự trích và ghi nhớ vài thông tin học sinh nhắc tới trong lúc trò chuyện (vd: đang học lớp mấy) để cá nhân hoá câu trả lời sau này — có toast nhỏ báo "Đã ghi nhớ: ..." ngay khi xảy ra. Học sinh có thể xoá toàn bộ bộ nhớ này bất cứ lúc nào bằng nút **"Xoá bộ nhớ AI của tôi"** trong Cài đặt.
- **Điểm thưởng (XP) & chuỗi ngày học**: mỗi lượt hỏi AI thành công được cộng XP, tính streak theo ngày (múi giờ VN, không tính 2 lượt cùng ngày là 2 ngày streak) — hiện ở góc sidebar. Có 4 thành tựu mở khoá tự động: 🧠 Bài học đầu tiên, 🔥 Chuỗi 7 ngày, 🏆 Chuỗi 30 ngày, 📚 100 câu hỏi — báo bằng toast khi vừa đạt được.

## 17. Thẻ ghi nhớ (Flashcards) + Trò chơi luyện tập + biểu tượng PWA (mới)

Nút mới **"Thẻ ghi nhớ & Trò chơi"** ngay dưới "Đoạn chat mới" ở sidebar, mở ra 1 màn hình riêng:

- **Bộ thẻ ghi nhớ**: tạo trống rồi tự thêm từng thẻ (mặt trước/mặt sau), hoặc bấm **"Tạo bằng AI ✨"** — chỉ cần nhập 1 chủ đề (vd: "Từ vựng tiếng Anh Unit 5", "Hằng đẳng thức đáng nhớ"), AI tự soạn 4-12 thẻ chỉ với **1 lượt gọi API** (không streaming, parse JSON kết quả), lỗi định dạng thì báo rõ để thử lại chứ không hiện thẻ rác.
- **Chế độ Học**: lật từng thẻ (bấm vào thẻ để lật), tự đánh giá "Đã nhớ"/"Chưa nhớ" — dùng kiểu **Leitner đơn giản** (đúng thì tăng mức độ nhớ tối đa 5, sai thì về mức 1 để ưu tiên ôn lại sớm hơn ở lượt học sau). Mức độ nhớ hiện luôn trên từng thẻ trong danh sách (Lv1-Lv5).
- **Trò chơi "Lật thẻ ghi nhớ"** (Memory Match): xáo mặt trước/mặt sau của tối đa 8 cặp thẻ thành lưới, tìm đúng cặp khớp nhau — có đếm thời gian + số lượt lật, thưởng XP khi thắng (điểm thưởng cao hơn nếu nhanh & ít lượt lật sai).
- Cả 2 chế độ đều nối vào hệ Điểm thưởng (XP)/Streak/Thành tựu có sẵn (mục 16): thêm 2 thành tựu mới 🗂️ **Bộ thẻ đầu tiên** và 🎮 **Người chơi mới**.
- API liên quan: `GET/POST /api/decks`, `POST /api/decks/generate`, `GET/PATCH/DELETE /api/decks/<id>`, `POST /api/decks/<id>/cards`, `PATCH/DELETE /api/cards/<id>`, `POST /api/games/complete`.

**Biểu tượng ứng dụng (PWA)**: đã gắn 2 icon bạn gửi vào `static/icons/`, thêm route `/manifest.json` (đã sửa tên trong đó từ "StudyMate AI Pro" thành **"StudyMate AI"** cho đúng quy tắc đặt tên ở mục 10) và các thẻ `<link rel="manifest">`/`theme-color`/`apple-touch-icon` vào `<head>` — giờ bấm "Cài đặt ứng dụng" / "Thêm vào màn hình chính" trên điện thoại sẽ hiện đúng icon + tên bạn cung cấp thay vì icon mặc định của trình duyệt.

⚠️ Chưa có: xoá bớt/đổi icon qua giao diện Admin (hiện phải tự thay file trong `static/icons/`), quiz trắc nghiệm tự sinh từ bộ thẻ (khác với chế độ Học lật thẻ), chia sẻ bộ thẻ giữa các tài khoản.

## 18. Sửa lỗi hiển thị ký hiệu toán học (căn bậc hai...) — báo bởi học sinh (mới, quan trọng)

Học sinh **BlackadaNutella** báo lỗi qua nút "Báo lỗi": câu trả lời hiện nguyên chữ `\( \sqrt{a} \)` thay vì ký hiệu căn bậc hai đẹp — "không đọc được".

**Nguyên nhân**: hệ render công thức (KaTeX) vốn hoạt động đúng, nhưng không có gì đảm bảo AI luôn viết LaTeX **cân bằng dấu ngoặc** hoặc không lỡ **chép lại y nguyên** ký hiệu bị lỗi/không rõ ràng mà học sinh gõ vào — khi 1 công thức bị lệch dấu, KaTeX không render được, để lộ nguyên cú pháp LaTeX thô ra màn hình.

**Đã sửa 2 lớp**:
1. **System prompt** (mọi chế độ, kể cả AI Tutor tuỳ chỉnh): thêm quy tắc bắt buộc tự kiểm tra số dấu mở = số dấu đóng trước khi trả lời, và **cấm chép lại y nguyên** ký hiệu toán học bị lỗi từ học sinh — phải tự hiểu ý rồi viết lại bằng LaTeX chuẩn hoặc bằng lời.
2. **Lưới an toàn phía trình duyệt** (`fallbackReadableMath()`, hàm mới): sau mỗi lần KaTeX cố render, quét lại phần tử — nếu vẫn còn sót cú pháp LaTeX thô (KaTeX không render được vì lý do gì đó), tự thay bằng ký hiệu Unicode dễ đọc (`\sqrt{a}` → `√(a)`, `\frac{a}{b}` → `(a)/(b)`, `\times`→`×`, `\pi`→`π`, `\le`→`≤`...) — học sinh **không bao giờ** phải nhìn thấy cú pháp LaTeX thô nữa, kể cả trong trường hợp xấu nhất. Đã kiểm thử kỹ bằng Node.js với đúng câu trong báo cáo lỗi, hoạt động chính xác.

## 19. Sổ lỗi sai (Mistake Book) (mới)

Theo đúng thứ tự ưu tiên bạn đề ra (AI Memory + Mistake Book làm trước tiên):

- Dưới mỗi câu trả lời AI, cạnh nút "Báo lỗi" có thêm nút **"Lưu vào Sổ lỗi sai"** — học sinh tự mô tả ngắn gọn lỗi mình vừa mắc (vd: "Chuyển vế quên đổi dấu"), kèm môn học.
- **Lỗi lặp lại** (cùng môn + cùng mô tả, đã chuẩn hoá hoa/thường) không tạo dòng mới — chỉ tăng số đếm, hiển thị đúng kiểu `Chuyển vế sai dấu ×3` như mô tả của bạn.
- Xem trong tab **"Sổ lỗi sai"** (cạnh tab "Thẻ ghi nhớ", cùng màn hình): nhóm theo môn học, lỗi lặp nhiều xếp lên đầu.
- Nút **"Ôn lại ngay"** trên mỗi lỗi: đóng Sổ lỗi sai, mở đoạn chat mới, tự chọn sẵn môn học + Chế độ "Luyện tập", điền sẵn câu hỏi nhờ AI ra 3 bài đúng dạng lỗi đó — biến việc "biết mình sai gì" thành hành động luyện tập ngay, không chỉ ghi chép suông.
- Đánh dấu "Đã khắc phục" khi không còn mắc lỗi đó nữa (ẩn khỏi danh sách chính, có thể mở lại).
- Nối vào hệ Điểm thưởng: +5 XP mỗi lần ghi nhận (kể cả lặp lại — tự nhận ra lỗi cũng đáng khích lệ), thành tựu mới 📕 **Tự nhận ra lỗi**.
- API: `GET/POST /api/mistakes`, `PATCH/DELETE /api/mistakes/<id>`.

⚠️ Đây là Sổ lỗi sai dựa trên **học sinh tự mô tả**, không phải AI tự động phát hiện và phân loại lỗi (việc đó cần AI phân tích riêng từng câu trả lời — có thể làm ở bản sau nếu bạn muốn, nhưng sẽ tốn thêm 1 lượt gọi API mỗi câu trả lời ở chế độ "Kiểm tra bài làm").

## Về roadmap dài hạn bạn chia sẻ

Đã đọc kỹ phần định hướng Phase 1-4 và danh sách 5 tính năng ưu tiên. Đồng ý hướng "AI hiểu người học, không chỉ hiểu câu hỏi" là lợi thế cạnh tranh hợp lý so với chép lại ChatGPT/Claude. Bộ nhớ AI (đã có) + Sổ lỗi sai (mục 19) là bước khởi đầu đúng hướng cho Phase 2. Quiz Generator, Study Plan, và AI Tutor Store (marketplace công khai cho Custom Tutor) vẫn **chưa làm** — mỗi cái là 1 hệ thống riêng khá lớn (Quiz Generator cần chấm điểm tự động + phân tích điểm yếu; Study Plan cần lịch trình đa ngày + tự điều chỉnh; AI Tutor Store cần thêm khái niệm "publish công khai" lên trên hệ Custom Tutor hiện chỉ có ở mức cá nhân/developer). Nói cụ thể bạn muốn làm cái nào tiếp theo, mình sẽ tập trung làm cho xong 1 cái thay vì làm dở cả 3.

## 20. Sửa lỗi crash "no such column: resolved_by" + rà soát toàn bộ schema database (mới, quan trọng)

Bạn báo lỗi crash thật khi bấm "Đánh dấu đã xử lý" ở `/developer/issues/2/resolve`: `sqlite3.OperationalError: no such column: resolved_by`.

**Nguyên nhân**: bảng `issue_reports` được tạo lần đầu (ở máy bạn) từ một phiên bản code CŨ, lúc đó cột `resolved_by` chưa tồn tại. Sau này code có thêm cột đó vào câu lệnh `CREATE TABLE IF NOT EXISTS` — nhưng vì bảng ĐÃ tồn tại rồi nên lệnh đó chỉ là no-op, không tự thêm cột mới vào bảng cũ. Đây là kiểu lỗi "schema drift" kinh điển khi phát triển thêm tính năng cho 1 database SQLite đã có dữ liệu.

**Đã sửa tận gốc, không chỉ vá 1 cột**: thêm hàm `ensure_columns()` — tự dò và thêm MỌI cột còn thiếu cho MỌI bảng, mỗi lần khởi động server, an toàn để chạy lại nhiều lần. Đã áp dụng cho toàn bộ 18 bảng trong app, không riêng `issue_reports`. Đã kiểm thử bằng cách **giả lập chính xác** database cũ của bạn (tạo bảng `issue_reports` thiếu cột `resolved_by`) rồi xác nhận: khởi động app lên → cột tự xuất hiện → bấm "Đánh dấu đã xử lý" chạy bình thường, không còn crash.

**Đã rà soát toàn bộ 67 route** trong app (kiểm thử tự động, không phải chỉ đọc code) — không phát hiện lỗi crash nào khác.

## 21. Sửa ký hiệu toán học lần 2 — đơn giản hoá lời dặn AI (mới)

Bạn gửi tiếp ảnh cho thấy vẫn còn vấn đề: câu trả lời có nhiều dấu ngoặc thừa kiểu `( √(a) )`, và 1 từ tiếng Anh lạc "monospaced" xuất hiện giữa câu trả lời tiếng Việt.

Lưới an toàn phía trình duyệt (mục 18) hoạt động đúng — đó là lý do không còn thấy cú pháp `\( \)` thô nữa. Nhưng lời dặn (system prompt) mình thêm ở mục 18 hơi dài dòng, giải thích quá chi tiết "nếu sai thì sẽ hiện lỗi gì" — nghi vấn là AI bắt chước phong cách dài dòng/cẩn trọng quá mức đó, dẫn tới thừa ngoặc + lạc từ. Đã **rút gọn đáng kể** lời dặn (còn 2 câu ngắn, không mô tả chi tiết trường hợp lỗi) ở cả 2 nơi (chế độ thường + AI Tutor tuỳ chỉnh).

⚠️ Thành thật lưu ý: đây là hành vi của model AI thật (xAI/Grok), mình không có cách nào gọi thử model thật từ môi trường đang code để kiểm chứng 100% trước khi giao cho bạn — khác với các lỗi code (Python/SQL/JS) mình LUÔN chạy thử và xác nhận trước khi báo đã sửa. Bạn thử lại và cho mình biết còn hiện tượng này không, nếu còn thì mình sẽ rút gọn lời dặn hơn nữa hoặc bỏ hẳn đoạn đó, chỉ dựa hoàn toàn vào lưới an toàn phía trình duyệt (vốn đã đảm bảo học sinh không bao giờ thấy cú pháp `\( \)` thô, dù có thừa ngoặc thì cũng không nghiêm trọng bằng).

## 22. Hiệu ứng ngọn lửa khi đạt mốc streak (mới)

Đúng như yêu cầu: khi số ngày học liên tục (streak) chạm mốc **3, 10, 30, 100, 200, 300, 500, 1000**, một hiệu ứng ngọn lửa 🔥 bùng lên giữa màn hình (không chỉ đổi số nhỏ ở sidebar), tự biến mất sau ~2.7 giây.

**"Ngọn lửa ngày càng đậm"**: mốc càng cao thì lửa càng "nặng đô" — nhiều lớp 🔥🔥🔥 hơn, kích thước lớn hơn, glow (quầng sáng) toả rộng và đậm hơn, màu ngả dần từ vàng (mốc 3) → cam (mốc 10-200) → đỏ (mốc 300) → tím-đỏ ở mốc 500-1000 (kèm 👑 cho 2 mốc huyền thoại này).

Chỉ bắn hiệu ứng khi streak **thực sự tăng lên đúng mốc trong phiên đang dùng** (không bắn lại mỗi lần tải trang nếu streak hiện tại tình cờ đang ở mốc từ hôm trước).

## 23. Quiz Generator + Study Plan — hoàn thành Phase 1 (mới)

Bạn gửi bản đặc tả sản phẩm rất lớn (41 mục — Memory, Mistake Book, Quiz, Study Plan, Teacher Mode, AI Tutor Store, Voice Mode, Screen Capture, Command Palette...). Đây là tầm nhìn nhiều năm cho 1 sản phẩm, không thể làm hết trong 1 lượt mà vẫn đảm bảo chất lượng — đặc biệt khi chính bản đặc tả đó yêu cầu "không tạo nút giả không hoạt động". Vì vậy mình tập trung hoàn thành nốt **Phase 1** theo đúng roadmap bạn tự đề ra: AI Memory ✅, Mistake Book ✅, Achievement + XP ✅ (đã có từ trước) — còn thiếu **Quiz Generator** và **Study Plan**, nay đã làm xong.

Cả 2 tính năng đều xuất hiện trong overlay "Thẻ ghi nhớ & Trò chơi" (đổi thành 4 tab: Thẻ ghi nhớ | Sổ lỗi sai | Quiz | Kế hoạch ôn tập).

### 📝 Quiz Generator
- AI tạo đề chỉ từ 1 chủ đề (hoặc dựa trên nội dung 1 đoạn chat có sẵn), chọn độ khó (Dễ/Trung bình/Khó/Nâng cao) và số câu.
- 3 dạng câu hỏi: **trắc nghiệm, đúng/sai, điền khuyết** — cố tình CHỈ chọn 3 dạng này vì chấm được tự động, chính xác 100%, không tốn thêm lượt gọi AI nào lúc chấm bài (so khớp đáp án đã chuẩn hoá). Dạng tự luận/ghép nối cần AI chấm chủ quan nên chưa hỗ trợ — xem phần "Chưa làm".
- Làm bài từng câu, nộp bài ra ngay: điểm số, % đúng, thời gian, **chủ đề còn yếu** (dựa trên câu sai, gom theo "topic" AI tự gắn cho từng câu), xem lại từng câu kèm giải thích.
- **Nối liền hệ sinh thái có sẵn**: câu trả lời sai tự động lưu vào Sổ lỗi sai (dùng chung logic gộp trùng lặp ở mục 19); hoàn thành quiz cộng XP, đạt 100% mở khoá thành tựu 💯 **Điểm tuyệt đối**.

### 🎯 Study Plan
- Nhập mục tiêu (vd: "Ôn thi Toán 8 trong 14 ngày") — AI chia thành việc cho từng ngày, ngày cuối luôn là ôn tập tổng hợp.
- Mỗi việc: **Hoàn thành** (✓, cộng XP), **Bỏ qua**, hoặc **Hỏi AI** (mở đoạn chat mới hỏi thẳng về chủ đề hôm đó — tái dùng đúng cơ chế "Ôn lại ngay" của Sổ lỗi sai).
- **Tự động phát hiện trễ tiến độ**: nếu có việc ở ngày đã qua mà vẫn "Chưa làm", nút **"Sắp xếp lại"** hiện ra — gọi AI phân bổ lại các việc CÒN THIẾU vào số ngày còn lại, giữ nguyên các việc đã hoàn thành (không mất tiến độ đã có). Đây chính là phần "tự điều chỉnh kế hoạch theo tiến độ" trong đặc tả của bạn.
- Hoàn thành trọn kế hoạch mở khoá thành tựu 🏆 **Về đích**.

Đã kiểm thử đầy đủ backend (sinh đề/kế hoạch, chấm điểm, gộp lỗi sai trùng lặp, sắp xếp lại kế hoạch giữ tiến độ) và toàn bộ giao diện render không lỗi, cộng với rà soát lại lần nữa toàn bộ 67+ route để đảm bảo không phát sinh lỗi mới.

## 24. Đăng nhập khách + Avatar + Quản lý tài khoản + Công cụ kiểm thử cho Developer (mới)

### 👤 Đăng nhập khách ("dùng thử ngay, không cần đăng ký")
- Nút mới ở trang đăng nhập, tạo 1 tài khoản khách THẬT trong DB (username dạng `khach_xxxxxxxx`, không có mật khẩu) — dùng lại toàn bộ hạ tầng sẵn có (chat, XP/streak, thẻ ghi nhớ, sổ lỗi sai...) mà không cần viết thêm code riêng cho "chế độ ẩn danh".
- Đánh dấu rõ bằng nhãn **"KHÁCH"** cạnh tên trong sidebar.
- **Tạo tài khoản chính thức bất cứ lúc nào** (mục "Tài khoản" trong Cài đặt) — chỉ cần đặt username + mật khẩu, dữ liệu đã dùng thử (đoạn chat, XP, thẻ ghi nhớ...) được **giữ nguyên 100%** vì thao tác này cập nhật thẳng lên dòng tài khoản hiện tại, không tạo tài khoản mới.
- Admin bật/tắt được từ `/developer` (mặc định BẬT, không cần cấu hình .env).
- ⚠️ Đánh đổi tất yếu của "khách": không có mật khẩu nên **mất tài khoản nếu xoá cookie trình duyệt** trước khi tạo tài khoản chính thức — đã ghi chú rõ ngay dưới nút đăng nhập khách.

### 🎨 Avatar + Quản lý tài khoản (mọi tài khoản đều dùng được)
- 16 avatar hình emoji (🦊🐱🐼🦁🐸🐧🦉🐢🐬🦄🐙🦋🐨🐯🐰🐳) trên nền gradient màu riêng — chọn trong Cài đặt, lưu ngay, hiện luôn ở sidebar. Chưa chọn thì vẫn về mặc định chữ cái đầu tên như trước (không đổi giao diện với ai chưa dùng tính năng này).
- **Đổi mật khẩu** ngay trong Cài đặt (xác thực đúng mật khẩu hiện tại trước khi đổi) — mục còn thiếu đã ghi chú ở các bản trước, nay bổ sung.
- Tài khoản đăng nhập Google: hiện ghi chú "không cần mật khẩu ở đây" thay vì hiện form đổi mật khẩu không dùng được.

### 🧪 Công cụ kiểm thử (Sandbox) — dành cho Developer
Panel mới trong `/developer`, mục đích: **test tính năng nhanh mà không cần chờ dữ liệu thật** (vd: không cần đợi dùng app 500 ngày liên tục mới thấy hiệu ứng streak mốc 500):
- **Chỉnh XP / streak trực tiếp** cho bất kỳ tài khoản nào (kể cả chính mình) — gán thẳng số XP, streak hiện tại, streak dài nhất, áp dụng ngay lập tức. Đã kiểm thử: tài khoản mục tiêu load lại trang thấy đúng cấp độ/XP mới ngay, và **xác nhận tài khoản KHÔNG PHẢI admin bị chặn** khi cố gọi thẳng route này (bảo mật đúng như các route admin khác).
- **Xem trước hiệu ứng ngọn lửa streak**: 8 nút bấm (mốc 3/10/30/100/200/300/500/1000), mỗi nút mở trang chat ở tab mới và tự bắn hiệu ứng ngay khi tải xong — CHỈ hiển thị hình ảnh, không đụng tới dữ liệu XP/streak thật của ai cả (an toàn để bấm thử thoải mái).

⚠️ "Chỉnh các chế độ" trong yêu cầu gốc của bạn hơi mơ hồ (không rõ ý là Chế độ suy nghĩ, Chế độ học tập, hay thứ khác) — hiện mình mới làm phần XP/streak (rõ nghĩa nhất, khớp với "để test... hiệu ứng"). Nếu ý bạn là 1 loại "chế độ" cụ thể khác, nói rõ hơn để mình bổ sung đúng.

## Chưa làm (nằm ngoài phạm vi yêu cầu lần này)
Phần đầu prompt gốc của bạn từng có yêu cầu dựng lại toàn bộ thành một sản phẩm Next.js/TypeScript quy mô lớn (nhiều trang, Dashboard, Blog, Pricing...). Bản cập nhật này vẫn giữ nguyên nền tảng Flask hiện có của bạn. Nếu bạn vẫn muốn bản Next.js quy mô lớn, đó sẽ là một dự án tách riêng — cho mình biết nếu bạn muốn triển khai.

### Về bản đặc tả 41 mục (StudyMate AI — full ecosystem)
Đã đọc kỹ toàn bộ. Ngoài Phase 1 (mục 23 ở trên), các phần sau **chưa làm** — liệt kê rõ để bạn quyết định cái nào làm tiếp, mỗi cái đều đủ lớn để là 1 đợt phát triển riêng:

- **Phase 2**: Teacher Mode (lớp học/giao bài/chấm/thống kê học sinh), Smart Notes (ghi chú có Markdown/LaTeX/tag/thư mục), Focus Mode (Pomodoro/nhạc nền/thống kê phiên học), Voice Mode (hội thoại bằng giọng nói 2 chiều).
- **Phase 3**: AI Tutor Store (marketplace công khai cho Custom Tutor — hiện Custom Tutor chỉ dùng nội bộ, dev tự tạo cho mình), Developer Platform đầy đủ (Webhooks, Knowledge Base, Deployments), Share System (link công khai xem lại bài giải/quiz), Analytics chi tiết (DAU/WAU/MAU, retention).
- **Phase 4**: Quick Launcher (Alt+Space mở cửa sổ nhanh — giới hạn kỹ thuật: trình duyệt KHÔNG cho web app đăng ký phím tắt toàn hệ điều hành, chỉ có thể bắt phím tắt khi tab đang mở), Screen Capture + OCR, Select-to-Explain (bôi đen văn bản ngoài trang để hỏi AI — cũng cần extension trình duyệt, không làm được thuần bằng web app), Command Palette (Ctrl+K).
- Rải rác trong đặc tả còn có: Matching/tự luận trong Quiz (cần AI chấm chủ quan, tốn thêm lượt gọi AI mỗi lần chấm — khác hẳn 3 dạng đã làm), Language Learning mode riêng, Onboarding hỏi đáp lúc đăng ký lần đầu, notification center, global search xuyên suốt mọi loại dữ liệu, leaderboard (opt-in).

Không cái nào trong số này bị bỏ quên — chỉ là cần bạn xác nhận thứ tự ưu tiên trước khi mình bắt tay vào, để tránh lặp lại tình huống làm dở nhiều thứ cùng lúc.

Vài ý tưởng hợp lý để làm tiếp sau này (chưa làm, vì nằm ngoài yêu cầu lần này):
- Trang đổi mật khẩu cho tài khoản đăng nhập bằng mật khẩu (hiện chỉ có thể đổi qua thao tác thủ công trong DB).
- Rate limiting cho `/login`, `/register` để chống spam/brute-force khi deploy công khai (gợi ý: `flask-limiter`).
- Lưu "Chế độ suy nghĩ" đang chọn vào tuỳ chọn cá nhân để nhớ qua các lần đăng nhập (xem ghi chú cuối mục 11).
- Gia hạn tự động trừ tiền định kỳ, hoá đơn/biên lai điện tử, hoàn tiền (xem ghi chú cuối mục 14).
- Quiz trắc nghiệm tự sinh + chấm điểm từ bộ thẻ ghi nhớ, và các ý tưởng game khác (xem ghi chú cuối mục 17).
- Study Plan (kế hoạch ôn tập đa ngày tự động), Quiz Generator (chấm điểm + phân tích điểm yếu), AI Tutor Store (marketplace công khai) — xem mục "Về roadmap dài hạn" ở trên.
- Quiz trắc nghiệm tự sinh + chấm điểm từ bộ thẻ ghi nhớ, và các ý tưởng game khác (đố vui theo thời gian, thi đấu giữa 2 học sinh...) — xem ghi chú cuối mục 17.
