import 'dotenv/config';
import { createExpressApp } from './infrastructure/server/expressApp';

const PORT = process.env.PORT || 3000;

const app = createExpressApp();

app.listen(PORT, () => {
  console.log(`[Sentinel-Graph] Node.js server started on http://localhost:${PORT}`);
  console.log(`[Sentinel-Graph] Python service: ${process.env.PYTHON_SERVICE_URL || 'http://localhost:8000'}`);
});
