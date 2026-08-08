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

> ⚠️ Lưu ý: bản `app.py` bạn gửi lần này là một nhánh phát triển khác với các bản trước (đã có sẵn hệ thống vai trò `user → developer → admin → super_admin`, AI Tutor tuỳ chỉnh, nhật ký audit, v.v. — xem mục 12). Mục 10–11 dưới đây là phần mình vừa hoàn thiện theo đúng yêu cầu mới nhất của bạn. Hai mục "Báo lỗi câu trả lời" / "Bộ nhớ AI" ở bản README cũ **không có trong nhánh `app.py` này** nên đã được gỡ khỏi tài liệu để tránh nhầm lẫn — nếu bạn vẫn muốn 2 tính năng đó trong nhánh này, cứ nhắn mình làm tiếp.

Đã bỏ hẳn chữ **"Pro"** khỏi mọi nơi hiển thị cho tài khoản thường — tên app giờ đổi động theo gói:

| Gói | Ai có | Giới hạn đọc file/ảnh mỗi 24h | Dung lượng tối đa/file | Tên app hiển thị |
|---|---|---|---|---|
| 🆓 **Free** | Mặc định mọi tài khoản mới | 20 lượt | 20MB | `StudyMate AI` |
| 💎 **Premium** | Admin gán tay từ `/developer` | 50 lượt | 500MB | `StudyMate AI Premium` |
| 🚀 **Max** | Admin gán tay, **hoặc tự động nếu vai trò ≥ Developer** | Không giới hạn | 1GB | `StudyMate AI Max` |

- Cột `plan` mới trong bảng `users` (mặc định `'free'`) lưu gói do Admin gán thủ công — **nhưng** tài khoản có vai trò `developer`/`admin`/`super_admin` luôn được tính là **Max vô điều kiện** ngay cả khi cột `plan` vẫn ghi `'free'` (hàm `effective_plan()` ưu tiên vai trò trước, không cần Admin phải gán tay cho từng dev). Vì vậy trang `/developer` **không cho đổi gói** với các tài khoản từ Developer trở lên (nút bị ẩn, kèm chú thích lý do).
- Giới hạn *số lượt* đọc file/ảnh tính theo **cửa sổ trượt 24 giờ** (không phải theo lịch nửa đêm reset) — bảng mới `file_uploads` ghi lại mỗi lượt tải lên, cứ quá 24h thì lượt đó "hết hạn" và tự nhường chỗ cho lượt mới. Giới hạn dung lượng còn siết luôn cả bước trích chữ từ PDF/Word: Free cắt ở ~12.000 ký tự, Premium ~48.000 ký tự, Max không cắt.
- **Đổi gói (Admin trở lên)**: bảng "Toàn bộ tài khoản" ở `/developer` có thêm cột **Gói** + dropdown đổi gói tại chỗ (`POST /developer/users/<id>/plan`), có ghi audit log.
- **Xem gói + hạn mức của chính mình**: `GET /api/plan` trả về gói hiện tại, đã dùng bao nhiêu/bao nhiêu lượt hôm nay, có phải "miễn phí theo vai trò" không. Hiển thị ngay trong **Cài đặt** (thanh tiến trình nhỏ, tự làm mới sau mỗi lần tải file).
- **Hộp thoại "Nâng cấp gói"** làm lại hoàn toàn: hiện đúng 3 cột Free/Premium/Max với số liệu thật (không còn số liệu giả), tự khoanh viền cột gói hiện tại của người xem, và liệt kê luôn các Chế độ suy nghĩ mở khoá ở từng gói (xem mục 11). Chưa có cổng thanh toán thật — bấm nút ở 2 cột còn lại chỉ hiện "Chưa khả dụng", đúng như bản trước.

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

## Chưa làm (nằm ngoài phạm vi yêu cầu lần này)
Phần đầu prompt gốc của bạn từng có yêu cầu dựng lại toàn bộ thành một sản phẩm Next.js/TypeScript quy mô lớn (nhiều trang, Dashboard, Blog, Pricing...). Bản cập nhật này vẫn giữ nguyên nền tảng Flask hiện có của bạn. Nếu bạn vẫn muốn bản Next.js quy mô lớn, đó sẽ là một dự án tách riêng — cho mình biết nếu bạn muốn triển khai.

Vài ý tưởng hợp lý để làm tiếp sau này (chưa làm, vì nằm ngoài yêu cầu lần này):
- Trang đổi mật khẩu cho tài khoản đăng nhập bằng mật khẩu (hiện chỉ có thể đổi qua thao tác thủ công trong DB).
- Rate limiting cho `/login`, `/register` để chống spam/brute-force khi deploy công khai (gợi ý: `flask-limiter`).
- Lưu "Chế độ suy nghĩ" đang chọn vào tuỳ chọn cá nhân để nhớ qua các lần đăng nhập (xem ghi chú cuối mục 11).
- Cổng thanh toán thật cho Premium/Max (hiện Admin chỉ gán gói thủ công, chưa có Stripe/VNPay/Momo...).
