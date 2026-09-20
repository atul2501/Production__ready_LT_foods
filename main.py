from app.main import app

__all__ = ["app"]



# cd ~/lt_fodds/Production__ready_LT_foods

# sed -i \
#   -e "s#User=invoice-service#User=ec2-user#" \
#   -e "s#Group=invoice-service#Group=ec2-user#" \
#   -e "s#/opt/invoice-service#$PWD#g" \
#   deploy/systemd/*.service

# sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
# sudo systemctl daemon-reload
# sudo systemctl restart invoice-api invoice-worker
# sudo systemctl status invoice-api.service --no-pager
